from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from agent.auxiliary_client import call_llm
from hermes_constants import get_hermes_dir, get_hermes_home
from tools.browser_backend_base import BrowserBackend, BrowserSessionState
from tools.browser_providers.base import CloudBrowserProvider
from tools.browser_providers.browser_use import BrowserUseProvider
from tools.browser_providers.browserbase import BrowserbaseProvider
from tools.browser_session_store import BrowserSessionStore

logger = logging.getLogger(__name__)

_DEFAULT_COMMAND_TIMEOUT = 30

# Standard PATH entries for environments with minimal PATH (e.g. systemd services).
# Includes macOS Homebrew paths (/opt/homebrew/* for Apple Silicon).
_SANE_PATH = (
    "/opt/homebrew/bin:/opt/homebrew/sbin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)

_PROVIDER_REGISTRY: dict[str, type[CloudBrowserProvider]] = {
    "browserbase": BrowserbaseProvider,
    "browser-use": BrowserUseProvider,
}


class AgentBrowserBackend(BrowserBackend):
    """Browser backend that wraps the existing `agent-browser` CLI flow."""

    def __init__(self) -> None:
        self._sessions: BrowserSessionStore[BrowserSessionState] = BrowserSessionStore()
        self._recording_sessions: set[str] = set()
        self._cached_cloud_provider: CloudBrowserProvider | None = None
        self._cloud_provider_resolved = False

    def backend_name(self) -> str:
        return "agent-browser"

    def is_configured(self) -> bool:
        try:
            self.find_agent_browser()
        except FileNotFoundError:
            return False

        provider = self._get_cloud_provider()
        if provider is not None and not provider.is_configured():
            return False
        return True

    def is_local(self) -> bool:
        # CDP attach can target a remote Chrome host; treat any active
        # BROWSER_CDP_URL override as non-local for SSRF guard enforcement.
        cdp_override_set = bool(os.environ.get("BROWSER_CDP_URL", "").strip())
        return self._get_cloud_provider() is None and not cdp_override_set

    def init_session(self, task_id: str) -> BrowserSessionState:
        task_id = task_id or "default"

        def _factory() -> BrowserSessionState:
            now = time.time()
            cdp_override = get_cdp_override()
            if cdp_override:
                session_info = _create_cdp_session(task_id, cdp_override)
            else:
                provider = self._get_cloud_provider()
                if provider is None:
                    session_info = _create_local_session(task_id)
                else:
                    session_info = provider.create_session(task_id)
            return BrowserSessionState(
                task_id=task_id,
                started_at=now,
                last_activity=now,
                current_url=None,
                metadata=session_info,
            )

        state = self._sessions.get_or_create(task_id, _factory)
        self._sessions.touch(task_id)
        return state

    def get_session(self, task_id: str) -> BrowserSessionState | None:
        return self._sessions.get(task_id)

    def list_sessions(self) -> list[BrowserSessionState]:
        return self._sessions.values()

    def close_session(self, task_id: str) -> bool:
        task_id = task_id or "default"
        state = self._sessions.get(task_id)
        if state is None:
            return False

        metadata = state.metadata
        self._maybe_stop_recording(task_id)

        try:
            self._run_agent_command(task_id, "close", [], timeout=10, create_if_missing=False)
        except Exception as exc:
            logger.debug("agent-browser close failed for task %s: %s", task_id, exc)

        self._sessions.remove(task_id)

        session_id = metadata.get("bb_session_id")
        provider = self._get_cloud_provider()
        if session_id and provider is not None:
            try:
                provider.close_session(str(session_id))
            except Exception as exc:
                logger.warning("Could not close cloud browser session %s: %s", session_id, exc)

        session_name = metadata.get("session_name")
        if session_name:
            socket_dir = os.path.join(_socket_safe_tmpdir(), f"agent-browser-{session_name}")
            if os.path.exists(socket_dir):
                pid_file = os.path.join(socket_dir, f"{session_name}.pid")
                if os.path.isfile(pid_file):
                    try:
                        daemon_pid = int(Path(pid_file).read_text().strip())
                        os.kill(daemon_pid, signal.SIGTERM)
                    except (ProcessLookupError, ValueError, PermissionError, OSError):
                        pass
                shutil.rmtree(socket_dir, ignore_errors=True)

        return True

    def emergency_cleanup(self, task_id: str) -> None:
        state = self._sessions.get(task_id)
        if state is None:
            return

        metadata = state.metadata
        session_id = metadata.get("bb_session_id")
        provider = self._get_cloud_provider()
        if session_id and provider is not None:
            try:
                provider.emergency_cleanup(str(session_id))
            except Exception:
                pass

        try:
            self.close_session(task_id)
        except Exception:
            pass

    def navigate(self, task_id: str, url: str) -> dict[str, Any]:
        task_id = task_id or "default"
        state = self.init_session(task_id)

        metadata = state.metadata
        is_first_nav = metadata.get("_first_nav", True)
        if is_first_nav:
            metadata["_first_nav"] = False
            self._maybe_start_recording(task_id)

        result = self._run_agent_command(
            task_id,
            "open",
            [url],
            timeout=max(self.get_command_timeout(), 60),
        )

        if not result.get("success"):
            return {
                "success": False,
                "error": result.get("error", "Navigation failed"),
            }

        data = result.get("data", {})
        title = data.get("title", "")
        final_url = data.get("url", url)
        state.current_url = final_url

        response: dict[str, Any] = {
            "success": True,
            "url": final_url,
            "title": title,
        }

        blocked_patterns = [
            "access denied",
            "access to this page has been denied",
            "blocked",
            "bot detected",
            "verification required",
            "please verify",
            "are you a robot",
            "captcha",
            "cloudflare",
            "ddos protection",
            "checking your browser",
            "just a moment",
            "attention required",
        ]
        title_lower = str(title).lower()
        if any(pattern in title_lower for pattern in blocked_patterns):
            response["bot_detection_warning"] = (
                f"Page title '{title}' suggests bot detection. The site may have blocked this request. "
                "Options: 1) Try adding delays between actions, 2) Access different pages first, "
                "3) Enable advanced stealth (BROWSERBASE_ADVANCED_STEALTH=true, requires Scale plan), "
                "4) Some sites have very aggressive bot detection that may be unavoidable."
            )

        if is_first_nav and "features" in metadata:
            features = metadata["features"] or {}
            active_features = [k for k, v in features.items() if v]
            if not features.get("proxies"):
                response["stealth_warning"] = (
                    "Running WITHOUT residential proxies. Bot detection may be more aggressive. "
                    "Consider upgrading Browserbase plan for proxy support."
                )
            response["stealth_features"] = active_features

        return response

    def snapshot(self, task_id: str, full: bool = False) -> dict[str, Any]:
        args = [] if full else ["-c"]
        result = self._run_agent_command(task_id, "snapshot", args)
        if not result.get("success"):
            return {
                "success": False,
                "error": result.get("error", "Failed to get snapshot"),
            }

        data = result.get("data", {})
        refs = data.get("refs", {})
        return {
            "success": True,
            "snapshot": data.get("snapshot", ""),
            "refs": refs,
            "element_count": len(refs) if isinstance(refs, dict) else 0,
        }

    def click(self, task_id: str, ref: str) -> dict[str, Any]:
        if not ref.startswith("@"):
            ref = f"@{ref}"
        result = self._run_agent_command(task_id, "click", [ref])
        if result.get("success"):
            return {"success": True, "clicked": ref}
        return {
            "success": False,
            "error": result.get("error", f"Failed to click {ref}"),
        }

    def type(self, task_id: str, ref: str, text: str) -> dict[str, Any]:
        if not ref.startswith("@"):
            ref = f"@{ref}"
        result = self._run_agent_command(task_id, "fill", [ref, text])
        if result.get("success"):
            return {"success": True, "typed": text, "element": ref}
        return {
            "success": False,
            "error": result.get("error", f"Failed to type into {ref}"),
        }

    def scroll(self, task_id: str, direction: str) -> dict[str, Any]:
        result = self._run_agent_command(task_id, "scroll", [direction])
        if result.get("success"):
            return {"success": True, "scrolled": direction}
        return {
            "success": False,
            "error": result.get("error", f"Failed to scroll {direction}"),
        }

    def press(self, task_id: str, key: str) -> dict[str, Any]:
        result = self._run_agent_command(task_id, "press", [key])
        if result.get("success"):
            return {"success": True, "pressed": key}
        return {
            "success": False,
            "error": result.get("error", f"Failed to press {key}"),
        }

    def back(self, task_id: str) -> dict[str, Any]:
        result = self._run_agent_command(task_id, "back", [])
        if result.get("success"):
            data = result.get("data", {})
            return {"success": True, "url": data.get("url", "")}
        return {
            "success": False,
            "error": result.get("error", "Failed to go back"),
        }

    def get_images(self, task_id: str) -> dict[str, Any]:
        js_code = """JSON.stringify(
            [...document.images].map(img => ({
                src: img.src,
                alt: img.alt || '',
                width: img.naturalWidth,
                height: img.naturalHeight
            })).filter(img => img.src && !img.src.startsWith('data:'))
        )"""
        result = self._run_agent_command(task_id, "eval", [js_code])
        if not result.get("success"):
            return {
                "success": False,
                "error": result.get("error", "Failed to get images"),
            }

        raw_result = result.get("data", {}).get("result", "[]")
        try:
            images = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
            if not isinstance(images, list):
                images = []
        except json.JSONDecodeError:
            return {
                "success": True,
                "images": [],
                "count": 0,
                "warning": "Could not parse image data",
            }

        return {
            "success": True,
            "images": images,
            "count": len(images),
        }

    def vision(self, task_id: str, question: str, annotate: bool = False) -> dict[str, Any]:
        screenshots_dir = get_hermes_dir("cache/screenshots", "browser_screenshots")
        screenshot_path = screenshots_dir / f"browser_screenshot_{uuid.uuid4().hex}.png"

        try:
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            _cleanup_old_screenshots(screenshots_dir, max_age_hours=24)

            screenshot_args: list[str] = []
            if annotate:
                screenshot_args.append("--annotate")
            screenshot_args.append("--full")
            screenshot_args.append(str(screenshot_path))

            result = self._run_agent_command(task_id, "screenshot", screenshot_args)
            if not result.get("success"):
                mode = "local" if self._get_cloud_provider() is None else "cloud"
                return {
                    "success": False,
                    "error": f"Failed to take screenshot ({mode} mode): {result.get('error', 'Unknown error')}",
                }

            actual = result.get("data", {}).get("path")
            if actual:
                screenshot_path = Path(actual)

            if not screenshot_path.exists():
                mode = "local" if self._get_cloud_provider() is None else "cloud"
                return {
                    "success": False,
                    "error": (
                        f"Screenshot file was not created at {screenshot_path} ({mode} mode). "
                        "This may indicate a socket path issue, a missing Chromium install "
                        "('agent-browser install'), or a stale daemon process."
                    ),
                }

            image_data = screenshot_path.read_bytes()
            image_base64 = base64.b64encode(image_data).decode("ascii")
            data_url = f"data:image/png;base64,{image_base64}"

            vision_prompt = (
                "You are analyzing a screenshot of a web browser.\n\n"
                f"User's question: {question}\n\n"
                "Provide a detailed and helpful answer based on what you see in the screenshot. "
                "If there are interactive elements, describe them. If there are verification challenges "
                "or CAPTCHAs, describe what type they are and what action might be needed. "
                "Focus on answering the user's specific question."
            )

            call_kwargs: dict[str, Any] = {
                "task": "vision",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": vision_prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                "max_tokens": 2000,
                "temperature": 0.1,
                "timeout": _vision_timeout_seconds(),
            }
            model = os.getenv("AUXILIARY_VISION_MODEL", "").strip()
            if model:
                call_kwargs["model"] = model

            response = call_llm(**call_kwargs)
            analysis = (response.choices[0].message.content or "").strip()

            from agent.redact import redact_sensitive_text

            analysis = redact_sensitive_text(analysis)
            output = {
                "success": True,
                "analysis": analysis or "Vision analysis returned no content.",
                "screenshot_path": str(screenshot_path),
            }
            if annotate and result.get("data", {}).get("annotations"):
                output["annotations"] = result["data"]["annotations"]
            return output
        except Exception as exc:
            logger.warning("browser_vision failed: %s", exc, exc_info=True)
            out: dict[str, Any] = {
                "success": False,
                "error": f"Error during vision analysis: {exc}",
            }
            if screenshot_path.exists():
                out["screenshot_path"] = str(screenshot_path)
                out["note"] = "Screenshot was captured but vision analysis failed. You can still share it via MEDIA:<path>."
            return out


    def solve_cloudflare(self, task_id: str, max_wait_seconds: int = 120) -> dict[str, Any]:
        return {"success": False, "error": "Not supported on this backend"}

    def console(self, task_id: str, clear: bool = False) -> dict[str, Any]:
        args = ["--clear"] if clear else []
        console_result = self._run_agent_command(task_id, "console", args)
        errors_result = self._run_agent_command(task_id, "errors", args)

        messages: list[dict[str, Any]] = []
        if console_result.get("success"):
            for msg in console_result.get("data", {}).get("messages", []):
                messages.append(
                    {
                        "type": msg.get("type", "log"),
                        "text": msg.get("text", ""),
                        "source": "console",
                    }
                )

        errors: list[dict[str, Any]] = []
        if errors_result.get("success"):
            for err in errors_result.get("data", {}).get("errors", []):
                errors.append(
                    {
                        "message": err.get("message", ""),
                        "source": "exception",
                    }
                )

        return {
            "success": True,
            "console_messages": messages,
            "js_errors": errors,
            "total_messages": len(messages),
            "total_errors": len(errors),
        }

    # ------------------------------------------------------------------
    # Compatibility helpers used by browser_tool and tests
    # ------------------------------------------------------------------

    def get_session_info(self, task_id: str | None = None) -> dict[str, Any]:
        task_id = task_id or "default"
        state = self.init_session(task_id)
        return state.metadata

    def run_command(
        self,
        task_id: str,
        command: str,
        args: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        return self._run_agent_command(task_id, command, args or [], timeout=timeout)

    def get_command_timeout(self) -> int:
        cfg = _load_browser_config()
        value = cfg.get("command_timeout")
        try:
            if value is not None:
                return max(int(value), 5)
        except Exception:
            pass
        return _DEFAULT_COMMAND_TIMEOUT

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    def _get_cloud_provider(self) -> CloudBrowserProvider | None:
        if self._cloud_provider_resolved:
            return self._cached_cloud_provider

        self._cloud_provider_resolved = True
        provider_key = _load_browser_config().get("cloud_provider")
        provider_cls = _PROVIDER_REGISTRY.get(provider_key or "")
        if provider_cls is not None:
            self._cached_cloud_provider = provider_cls()
        return self._cached_cloud_provider

    def _run_agent_command(
        self,
        task_id: str,
        command: str,
        args: list[str] | None = None,
        timeout: int | None = None,
        *,
        create_if_missing: bool = True,
    ) -> dict[str, Any]:
        args = args or []
        timeout = timeout or self.get_command_timeout()

        try:
            browser_cmd = self.find_agent_browser()
        except FileNotFoundError as exc:
            return {"success": False, "error": str(exc)}

        from tools.interrupt import is_interrupted

        if is_interrupted():
            return {"success": False, "error": "Interrupted"}

        if create_if_missing:
            state = self.init_session(task_id)
        else:
            state = self.get_session(task_id)
            if state is None:
                return {"success": False, "error": "No browser session for task"}

        session_info = state.metadata
        session_name = str(session_info.get("session_name") or "")
        if not session_name:
            return {"success": False, "error": "Missing agent-browser session_name"}

        if session_info.get("cdp_url"):
            backend_args = ["--cdp", str(session_info["cdp_url"])]
        else:
            backend_args = ["--session", session_name]

        cmd_parts = browser_cmd.split() + backend_args + ["--json", command] + args

        try:
            task_socket_dir = os.path.join(_socket_safe_tmpdir(), f"agent-browser-{session_name}")
            os.makedirs(task_socket_dir, mode=0o700, exist_ok=True)

            browser_env = dict(os.environ)
            browser_env["PATH"] = _build_browser_path(browser_env.get("PATH", ""))
            browser_env["AGENT_BROWSER_SOCKET_DIR"] = task_socket_dir

            stdout_path = os.path.join(task_socket_dir, f"_stdout_{command}")
            stderr_path = os.path.join(task_socket_dir, f"_stderr_{command}")
            stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                proc = subprocess.Popen(
                    cmd_parts,
                    stdout=stdout_fd,
                    stderr=stderr_fd,
                    stdin=subprocess.DEVNULL,
                    env=browser_env,
                )
            finally:
                os.close(stdout_fd)
                os.close(stderr_fd)

            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return {
                    "success": False,
                    "error": f"Command timed out after {timeout} seconds",
                }

            with open(stdout_path, "r", encoding="utf-8", errors="replace") as f:
                stdout = f.read()
            with open(stderr_path, "r", encoding="utf-8", errors="replace") as f:
                stderr = f.read()
            returncode = proc.returncode

            for p in (stdout_path, stderr_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

            stdout_text = stdout.strip()
            if stdout_text:
                try:
                    parsed = json.loads(stdout_text)
                    self._sessions.touch(task_id)
                    return parsed
                except json.JSONDecodeError:
                    raw = stdout_text[:2000]
                    if command == "screenshot":
                        recovered_path = extract_screenshot_path_from_text("\n".join([stdout_text, stderr.strip()]))
                        if recovered_path and Path(recovered_path).exists():
                            self._sessions.touch(task_id)
                            return {
                                "success": True,
                                "data": {
                                    "path": recovered_path,
                                    "raw": raw,
                                },
                            }
                    return {
                        "success": False,
                        "error": f"Non-JSON output from agent-browser for '{command}': {raw}",
                    }

            if returncode != 0:
                error_msg = stderr.strip() if stderr else f"Command failed with code {returncode}"
                return {"success": False, "error": error_msg}

            self._sessions.touch(task_id)
            return {"success": True, "data": {}}
        except Exception as exc:
            logger.warning("agent-browser command '%s' exception: %s", command, exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def _maybe_start_recording(self, task_id: str) -> None:
        if task_id in self._recording_sessions:
            return

        record_enabled = bool(_load_browser_config().get("record_sessions", False))
        if not record_enabled:
            return

        recordings_dir = get_hermes_home() / "browser_recordings"
        recordings_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_old_recordings(max_age_hours=72)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        recording_path = recordings_dir / f"session_{timestamp}_{task_id[:16]}.webm"

        result = self._run_agent_command(task_id, "record", ["start", str(recording_path)])
        if result.get("success"):
            self._recording_sessions.add(task_id)
            logger.info("Auto-recording browser session %s to %s", task_id, recording_path)

    def _maybe_stop_recording(self, task_id: str) -> None:
        if task_id not in self._recording_sessions:
            return

        try:
            self._run_agent_command(task_id, "record", ["stop"])
        except Exception:
            pass
        finally:
            self._recording_sessions.discard(task_id)

    @staticmethod
    def discover_homebrew_node_dirs() -> list[str]:
        return _discover_homebrew_node_dirs()

    @staticmethod
    def find_agent_browser() -> str:
        return find_agent_browser()


def _load_browser_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        browser_cfg = cfg.get("browser", {})
        if isinstance(browser_cfg, dict):
            return browser_cfg
    except Exception:
        pass

    config_path = get_hermes_home() / "config.yaml"
    if config_path.exists():
        try:
            import yaml

            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            browser_cfg = cfg.get("browser", {})
            if isinstance(browser_cfg, dict):
                return browser_cfg
        except Exception:
            pass
    return {}


def _create_local_session(task_id: str) -> dict[str, Any]:
    session_name = f"h_{uuid.uuid4().hex[:10]}"
    logger.info("Created local browser session %s for task %s", session_name, task_id)
    return {
        "session_name": session_name,
        "bb_session_id": None,
        "cdp_url": None,
        "features": {"local": True},
    }


def _create_cdp_session(task_id: str, cdp_url: str) -> dict[str, Any]:
    session_name = f"cdp_{uuid.uuid4().hex[:10]}"
    logger.info("Created CDP browser session %s -> %s for task %s", session_name, cdp_url, task_id)
    return {
        "session_name": session_name,
        "bb_session_id": None,
        "cdp_url": cdp_url,
        "features": {"cdp_override": True},
    }


def _discover_homebrew_node_dirs() -> list[str]:
    dirs: list[str] = []
    homebrew_opt = "/opt/homebrew/opt"
    if not os.path.isdir(homebrew_opt):
        return dirs

    try:
        for entry in os.listdir(homebrew_opt):
            if entry.startswith("node") and entry != "node":
                bin_dir = os.path.join(homebrew_opt, entry, "bin")
                if os.path.isdir(bin_dir):
                    dirs.append(bin_dir)
    except OSError:
        pass
    return dirs


def _build_browser_path(existing_path: str) -> str:
    hermes_node_bin = str(get_hermes_home() / "node" / "bin")
    path_parts = [p for p in existing_path.split(":") if p]
    candidate_dirs = (
        [hermes_node_bin]
        + _discover_homebrew_node_dirs()
        + [p for p in _SANE_PATH.split(":") if p]
    )
    for part in reversed(candidate_dirs):
        if os.path.isdir(part) and part not in path_parts:
            path_parts.insert(0, part)
    return ":".join(path_parts)


def find_agent_browser() -> str:
    which_result = shutil.which("agent-browser")
    if which_result:
        return which_result

    extra_dirs: list[str] = []
    for d in ["/opt/homebrew/bin", "/usr/local/bin"]:
        if os.path.isdir(d):
            extra_dirs.append(d)
    extra_dirs.extend(_discover_homebrew_node_dirs())

    hermes_node_bin = str(get_hermes_home() / "node" / "bin")
    if os.path.isdir(hermes_node_bin):
        extra_dirs.append(hermes_node_bin)

    if extra_dirs:
        which_result = shutil.which("agent-browser", path=os.pathsep.join(extra_dirs))
        if which_result:
            return which_result

    repo_root = Path(__file__).resolve().parents[2]
    local_bin = repo_root / "node_modules" / ".bin" / "agent-browser"
    if local_bin.exists():
        return str(local_bin)

    npx_path = shutil.which("npx")
    if not npx_path and extra_dirs:
        npx_path = shutil.which("npx", path=os.pathsep.join(extra_dirs))
    if npx_path:
        return "npx agent-browser"

    raise FileNotFoundError(
        "agent-browser CLI not found. Install it with: npm install -g agent-browser\n"
        "Or run 'npm install' in the repo root to install locally.\n"
        "Or ensure npx is available in your PATH."
    )


def _socket_safe_tmpdir() -> str:
    if sys.platform == "darwin":
        return "/tmp"
    return tempfile.gettempdir()


def extract_screenshot_path_from_text(text: str) -> str | None:
    if not text:
        return None

    patterns = [
        r"Screenshot saved to ['\"](?P<path>/[^'\"]+?\.png)['\"]",
        r"Screenshot saved to (?P<path>/\S+?\.png)(?:\s|$)",
        r"(?P<path>/\S+?\.png)(?:\s|$)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            path = match.group("path").strip().strip("'\"")
            if path:
                return path
    return None


def resolve_cdp_override(cdp_url: str) -> str:
    raw = (cdp_url or "").strip()
    if not raw:
        return ""

    lowered = raw.lower()
    if "/devtools/browser/" in lowered:
        return raw

    discovery_url = raw
    if lowered.startswith("ws://") or lowered.startswith("wss://"):
        if (
            raw.count(":") == 2
            and raw.rstrip("/").rsplit(":", 1)[-1].isdigit()
            and "/" not in raw.split(":", 2)[-1]
        ):
            discovery_url = (
                "http://" if lowered.startswith("ws://") else "https://"
            ) + raw.split("://", 1)[1]
        else:
            return raw

    version_url = (
        discovery_url
        if discovery_url.lower().endswith("/json/version")
        else discovery_url.rstrip("/") + "/json/version"
    )

    try:
        response = requests.get(version_url, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return raw

    ws_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
    return ws_url or raw


def get_cdp_override() -> str:
    return resolve_cdp_override(os.environ.get("BROWSER_CDP_URL", ""))


def _vision_timeout_seconds() -> float:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        val = cfg.get("auxiliary", {}).get("vision", {}).get("timeout")
        if val is not None:
            return float(val)
    except Exception:
        pass
    return 120.0


def _cleanup_old_screenshots(screenshots_dir: Path, max_age_hours: int = 24) -> None:
    try:
        cutoff = time.time() - (max_age_hours * 3600)
        for f in screenshots_dir.glob("browser_screenshot_*.png"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _cleanup_old_recordings(max_age_hours: int = 72) -> None:
    try:
        recordings_dir = get_hermes_home() / "browser_recordings"
        if not recordings_dir.exists():
            return
        cutoff = time.time() - (max_age_hours * 3600)
        for f in recordings_dir.glob("session_*.webm"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


