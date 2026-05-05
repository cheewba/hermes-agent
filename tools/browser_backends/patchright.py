from __future__ import annotations

import atexit
import base64
import importlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import concurrent.futures
import functools
import threading

def _run_in_pw_thread(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if not hasattr(self, '_executor'):
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="PWThread")
        if threading.current_thread().name.startswith("PWThread"):
            return func(self, *args, **kwargs)
        return self._executor.submit(func, self, *args, **kwargs).result()
    return wrapper

from urllib.parse import unquote, urlsplit

from agent.auxiliary_client import call_llm
from hermes_constants import get_hermes_dir, get_hermes_home
from tools.browser_backend_base import BrowserBackend, BrowserSessionState, ElementRef
from tools.browser_element_resolution import (
    ELEMENT_EXTRACTION_SCRIPT,
    INTERACTIVE_SELECTOR,
    resolve_element_locator,
    stale_ref_error,
)
from tools.browser_backends.agent_browser import resolve_cdp_override
from tools.browser_session_store import BrowserSessionStore
from tools.browser_snapshot import normalize_ref, render_snapshot

logger = logging.getLogger(__name__)


_XVFB_PROCESS: subprocess.Popen | None = None
_XVFB_DISPLAY: str | None = None
_XVFB_LOCK = threading.Lock()

_PATCHRIGHT_PROXY_OVERRIDES: dict[str, dict[str, Any]] = {}
_PATCHRIGHT_PROXY_LOCK = threading.Lock()


def _stop_xvfb() -> None:
    global _XVFB_PROCESS, _XVFB_DISPLAY
    with _XVFB_LOCK:
        proc = _XVFB_PROCESS
        _XVFB_PROCESS = None
        _XVFB_DISPLAY = None

    if proc is None:
        return

    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


atexit.register(_stop_xvfb)


@dataclass
class PatchrightRuntimeSession(BrowserSessionState):
    playwright: Any | None = None
    browser: Any | None = None
    browser_context: Any | None = None
    page: Any | None = None
    user_data_dir: str | None = None
    screenshot_dir: str | None = None
    console_messages: list[dict[str, Any]] = field(default_factory=list)
    js_errors: list[dict[str, Any]] = field(default_factory=list)


def _load_patchright_sync_playwright():
    candidates = [
        "patchright.sync_api",
        "patchright.sync_api._generated",
    ]
    for name in candidates:
        try:
            mod = importlib.import_module(name)
            if hasattr(mod, "sync_playwright"):
                return mod.sync_playwright
        except Exception:
            continue
    return None


_SYNC_PLAYWRIGHT = _load_patchright_sync_playwright()


class PatchrightBackend(BrowserBackend):
    """Native Patchright backend (no agent-browser CLI).

    Launch modes:
    - CDP attach mode when browser.patchright.cdp_url (or BROWSER_CDP_URL) is set.
    - Local launch mode otherwise (optionally with executable_path and xvfb settings).
    """

    def __init__(self) -> None:
        self._sessions: BrowserSessionStore[PatchrightRuntimeSession] = BrowserSessionStore()

    def backend_name(self) -> str:
        return "patchright"

    def is_configured(self) -> bool:
        return _SYNC_PLAYWRIGHT is not None

    def is_local(self) -> bool:
        # CDP attach can point at a remote browser process, so we must enforce
        # SSRF guards whenever CDP mode is configured.
        cfg = _patchright_config()
        return not bool(_resolve_patchright_cdp_url(cfg))

    def supports_runtime_proxy(self, task_id: str | None = None) -> bool:
        cfg = _patchright_config(task_id)
        return not bool(_resolve_patchright_cdp_url(cfg))

    def set_runtime_proxy(self, task_id: str, proxy: dict[str, Any] | None) -> None:
        if not self.supports_runtime_proxy(task_id):
            raise RuntimeError("Runtime proxy overrides are not supported in patchright CDP mode")
        _set_patchright_proxy_override(task_id, proxy)

    @_run_in_pw_thread
    def init_session(self, task_id: str) -> BrowserSessionState:
        task_id = task_id or "default"

        def _factory() -> PatchrightRuntimeSession:
            if _SYNC_PLAYWRIGHT is None:
                raise RuntimeError(
                    "Patchright backend requires Python package 'patchright'. "
                    "Install it and browser binaries before using browser.backend=patchright."
                )

            now = time.time()
            cfg = _patchright_config(task_id)
            cdp_url = _resolve_patchright_cdp_url(cfg)
            if not cdp_url:
                # DISPLAY must be ready before starting the Playwright/Patchright driver process.
                _ensure_xvfb_for_patchright(cfg)

            playwright = _SYNC_PLAYWRIGHT().start()
            timeout_ms = _timeout_ms(cfg.get("launch_timeout_ms"), default_ms=30_000)

            browser = None
            user_data_dir: str | None = None
            cdp_created_context = False
            if cdp_url:
                connect_kwargs: dict[str, Any] = {}
                if timeout_ms:
                    connect_kwargs["timeout"] = timeout_ms
                browser = playwright.chromium.connect_over_cdp(cdp_url, **connect_kwargs)
                contexts = list(getattr(browser, "contexts", []) or [])
                if contexts:
                    context = contexts[0]
                else:
                    context = browser.new_context()
                    cdp_created_context = True
            else:
                user_data_base = Path(cfg.get("user_data_base") or (get_hermes_home() / "cache" / "patchright_profiles"))
                user_data_base.mkdir(parents=True, exist_ok=True)
                user_data_dir = tempfile.mkdtemp(prefix=f"session_{task_id[:16]}_", dir=str(user_data_base))

                launch_kwargs: dict[str, Any] = {
                    "headless": bool(cfg.get("headless", True)),
                }
                channel = cfg.get("channel")
                if channel:
                    launch_kwargs["channel"] = channel
                executable_path = str(cfg.get("executable_path") or "").strip()
                if executable_path:
                    launch_kwargs["executable_path"] = executable_path
                if timeout_ms:
                    launch_kwargs["timeout"] = timeout_ms

                proxy_settings = _resolve_patchright_proxy(cfg)
                if proxy_settings:
                    launch_kwargs["proxy"] = proxy_settings

                context = playwright.chromium.launch_persistent_context(user_data_dir=user_data_dir, **launch_kwargs)

            cdp_created_page = False
            if cdp_url:
                # Never commandeer an existing CDP tab; create a dedicated page
                # for this Hermes session.
                page = context.new_page()
                cdp_created_page = True
            elif context.pages:
                page = context.pages[0]
            else:
                page = context.new_page()
                cdp_created_page = True

            state = PatchrightRuntimeSession(
                task_id=task_id,
                started_at=now,
                last_activity=now,
                current_url=page.url,
                playwright=playwright,
                browser=browser,
                browser_context=context,
                page=page,
                user_data_dir=user_data_dir,
                screenshot_dir=str(get_hermes_dir("cache/screenshots", "browser_screenshots")),
                metadata={
                    "mode": "cdp" if cdp_url else "launch",
                    "cdp_url": cdp_url,
                    "cdp_created_context": cdp_created_context,
                    "cdp_created_page": cdp_created_page,
                },
            )
            _attach_console_listeners(state)
            return state

        state = self._sessions.get_or_create(task_id, _factory)
        self._sessions.touch(task_id)
        return state

    def get_session(self, task_id: str) -> BrowserSessionState | None:
        return self._sessions.get(task_id)

    def list_sessions(self) -> list[BrowserSessionState]:
        return list(self._sessions.values())

    @_run_in_pw_thread
    def close_session(self, task_id: str) -> bool:
        task_id = task_id or "default"
        # Runtime proxy overrides are session-scoped; clear on close so a new
        # session does not silently inherit stale routing.
        _set_patchright_proxy_override(task_id, None)

        state = self._sessions.remove(task_id)
        if state is None:
            return False

        _safe_close_patchright_state(state)
        return True

    @_run_in_pw_thread
    def emergency_cleanup(self, task_id: str) -> None:
        try:
            self.close_session(task_id)
        except Exception:
            logger.debug("Patchright emergency cleanup failed for %s", task_id, exc_info=True)

    @_run_in_pw_thread
    def navigate(self, task_id: str, url: str) -> dict[str, Any]:
        session = self._get_runtime(task_id)
        if isinstance(session, dict):
            return session

        cfg = _patchright_config()
        timeout_ms = _action_timeout_ms(cfg)
        try:
            session.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            session.current_url = session.page.url
            self._sessions.touch(task_id)
            return {
                "success": True,
                "url": session.page.url,
                "title": session.page.title(),
            }
        except Exception as exc:
            return {"success": False, "error": f"Navigation failed: {exc}"}

    @_run_in_pw_thread
    def snapshot(self, task_id: str, full: bool = False) -> dict[str, Any]:
        session = self._get_runtime(task_id)
        if isinstance(session, dict):
            return session

        try:
            normalized: list[ElementRef] = []
            page_text_chunks: list[str] = []

            for frame, frame_path in _iter_frames(session.page):
                extracted = frame.evaluate(
                    ELEMENT_EXTRACTION_SCRIPT,
                    {"selector": INTERACTIVE_SELECTOR, "includeText": bool(full)},
                )
                if not isinstance(extracted, dict):
                    continue

                if full:
                    text_chunk = str(extracted.get("page_text") or "").strip()
                    if text_chunk:
                        page_text_chunks.append(text_chunk)

                raw_elements = extracted.get("elements") or []
                if not isinstance(raw_elements, list):
                    continue

                for raw in raw_elements:
                    if not isinstance(raw, dict):
                        continue
                    normalized.append(
                        ElementRef(
                            ref="",
                            role=_clean_text(raw.get("role")),
                            name=_clean_text(raw.get("name")),
                            text=_clean_text(raw.get("text")),
                            selector=_clean_text(raw.get("selector")),
                            xpath=_clean_text(raw.get("xpath")),
                            frame_path=list(frame_path),
                            bbox=raw.get("bbox") if isinstance(raw.get("bbox"), dict) else None,
                            visible=bool(raw.get("visible", True)),
                            enabled=bool(raw.get("enabled", True)),
                            attributes=raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {},
                        )
                    )

            ref_map: dict[str, ElementRef] = {}
            for idx, element in enumerate(normalized, start=1):
                element.ref = f"@e{idx}"
                ref_map[element.ref] = element

            session.ref_map = ref_map
            session.ref_version += 1
            session.current_url = session.page.url
            self._sessions.touch(task_id)

            snapshot_text = render_snapshot(
                ref_map.values(),
                full=full,
                page_text="\n\n".join(page_text_chunks),
            )
            return {
                "success": True,
                "snapshot": snapshot_text,
                "refs": _refs_payload(ref_map),
                "element_count": len(ref_map),
            }
        except Exception as exc:
            return {"success": False, "error": f"Failed to get snapshot: {exc}"}

    @_run_in_pw_thread
    def click(self, task_id: str, ref: str) -> dict[str, Any]:
        session = self._get_runtime(task_id)
        if isinstance(session, dict):
            return session

        target = session.ref_map.get(normalize_ref(ref))
        if target is None:
            return stale_ref_error()

        locator, _ = resolve_element_locator(session.page, target)
        if locator is None:
            return stale_ref_error()

        try:
            locator.scroll_into_view_if_needed(timeout=_action_timeout_ms(_patchright_config()))
            locator.click(timeout=_action_timeout_ms(_patchright_config()))
            session.current_url = session.page.url
            self._sessions.touch(task_id)
            return {"success": True, "clicked": target.ref}
        except Exception as exc:
            return {"success": False, "error": f"Failed to click {target.ref}: {exc}"}

    @_run_in_pw_thread
    def type(self, task_id: str, ref: str, text: str) -> dict[str, Any]:
        session = self._get_runtime(task_id)
        if isinstance(session, dict):
            return session

        target = session.ref_map.get(normalize_ref(ref))
        if target is None:
            return stale_ref_error()

        locator, _ = resolve_element_locator(session.page, target)
        if locator is None:
            return stale_ref_error()

        try:
            locator.scroll_into_view_if_needed(timeout=_action_timeout_ms(_patchright_config()))
            locator.fill(text, timeout=_action_timeout_ms(_patchright_config()))
            session.current_url = session.page.url
            self._sessions.touch(task_id)
            return {"success": True, "typed": text, "element": target.ref}
        except Exception as exc:
            return {"success": False, "error": f"Failed to type into {target.ref}: {exc}"}

    @_run_in_pw_thread
    def scroll(self, task_id: str, direction: str) -> dict[str, Any]:
        session = self._get_runtime(task_id)
        if isinstance(session, dict):
            return session

        delta = 900 if direction == "down" else -900
        try:
            session.page.mouse.wheel(0, delta)
            self._sessions.touch(task_id)
            return {"success": True, "scrolled": direction}
        except Exception as exc:
            return {"success": False, "error": f"Failed to scroll {direction}: {exc}"}

    @_run_in_pw_thread
    def press(self, task_id: str, key: str) -> dict[str, Any]:
        session = self._get_runtime(task_id)
        if isinstance(session, dict):
            return session

        try:
            session.page.keyboard.press(key)
            self._sessions.touch(task_id)
            return {"success": True, "pressed": key}
        except Exception as exc:
            return {"success": False, "error": f"Failed to press {key}: {exc}"}

    @_run_in_pw_thread
    def back(self, task_id: str) -> dict[str, Any]:
        session = self._get_runtime(task_id)
        if isinstance(session, dict):
            return session

        try:
            session.page.go_back(wait_until="domcontentloaded", timeout=_action_timeout_ms(_patchright_config()))
        except Exception:
            # Some pages have no history — still return current URL.
            pass

        try:
            current_url = session.page.url
        except Exception:
            current_url = session.current_url or ""

        session.current_url = current_url
        self._sessions.touch(task_id)
        return {"success": True, "url": current_url}

    @_run_in_pw_thread
    def get_images(self, task_id: str) -> dict[str, Any]:
        session = self._get_runtime(task_id)
        if isinstance(session, dict):
            return session

        images: list[dict[str, Any]] = []
        try:
            for frame, frame_path in _iter_frames(session.page):
                rows = frame.evaluate(
                    """
                    () => [...document.images].map(img => ({
                      src: img.src || '',
                      alt: img.alt || '',
                      width: img.naturalWidth || 0,
                      height: img.naturalHeight || 0,
                    })).filter(i => i.src && !i.src.startsWith('data:'))
                    """
                )
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    row = dict(row)
                    row["frame_path"] = list(frame_path)
                    images.append(row)

            self._sessions.touch(task_id)
            return {
                "success": True,
                "images": images,
                "count": len(images),
            }
        except Exception as exc:
            return {"success": False, "error": f"Failed to get images: {exc}"}

    @_run_in_pw_thread
    def vision(self, task_id: str, question: str, annotate: bool = False) -> dict[str, Any]:
        session = self._get_runtime(task_id)
        if isinstance(session, dict):
            return session

        screenshots_dir = Path(session.screenshot_dir or str(get_hermes_dir("cache/screenshots", "browser_screenshots")))
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_old_screenshots(screenshots_dir)

        screenshot_path = screenshots_dir / f"browser_screenshot_{uuid.uuid4().hex}.png"
        annotation_ids: list[str] = []

        try:
            annotations = None
            if annotate:
                snapshot_result = self.snapshot(task_id, full=False)
                if snapshot_result.get("success"):
                    annotations, annotation_ids = _inject_annotation_overlays(session)

            session.page.screenshot(path=str(screenshot_path), full_page=True)
            image_data = screenshot_path.read_bytes()
            image_base64 = base64.b64encode(image_data).decode("ascii")
            data_url = f"data:image/png;base64,{image_base64}"

            prompt = (
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
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                "max_tokens": 2000,
                "temperature": 0.1,
                "timeout": _vision_timeout_seconds(),
            }
            model = _patchright_config().get("vision_model")
            if model:
                call_kwargs["model"] = model

            response = call_llm(**call_kwargs)
            analysis = (response.choices[0].message.content or "").strip()

            from agent.redact import redact_sensitive_text

            analysis = redact_sensitive_text(analysis)
            result: dict[str, Any] = {
                "success": True,
                "analysis": analysis or "Vision analysis returned no content.",
                "screenshot_path": str(screenshot_path),
            }
            if annotate and annotations:
                result["annotations"] = annotations
            self._sessions.touch(task_id)
            return result
        except Exception as exc:
            out: dict[str, Any] = {"success": False, "error": f"Error during vision analysis: {exc}"}
            if screenshot_path.exists():
                out["screenshot_path"] = str(screenshot_path)
            return out
        finally:
            _remove_annotation_overlays(session, annotation_ids)

    @_run_in_pw_thread
    def console(self, task_id: str, clear: bool = False) -> dict[str, Any]:
        session = self._get_runtime(task_id)
        if isinstance(session, dict):
            return session

        messages = list(session.console_messages)
        errors = list(session.js_errors)
        if clear:
            session.console_messages.clear()
            session.js_errors.clear()

        self._sessions.touch(task_id)
        return {
            "success": True,
            "console_messages": messages,
            "js_errors": errors,
            "total_messages": len(messages),
            "total_errors": len(errors),
        }

    def _get_runtime(self, task_id: str) -> PatchrightRuntimeSession | dict[str, Any]:
        task_id = task_id or "default"
        try:
            state = self.init_session(task_id)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        if not isinstance(state, PatchrightRuntimeSession) or state.page is None:
            return {"success": False, "error": "Patchright session is not initialized"}
        return state


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _set_patchright_proxy_override(task_id: str, proxy: dict[str, Any] | None) -> None:
    task = str(task_id or "default")
    with _PATCHRIGHT_PROXY_LOCK:
        if proxy is None:
            _PATCHRIGHT_PROXY_OVERRIDES.pop(task, None)
            return
        _PATCHRIGHT_PROXY_OVERRIDES[task] = dict(proxy)


def _get_patchright_proxy_override(task_id: str) -> dict[str, Any] | None:
    task = str(task_id or "default")
    with _PATCHRIGHT_PROXY_LOCK:
        existing = _PATCHRIGHT_PROXY_OVERRIDES.get(task)
        return dict(existing) if isinstance(existing, dict) else None


def _patchright_config(task_id: str | None = None) -> dict[str, Any]:
    merged: dict[str, Any] = {"headless": True}
    try:
        from hermes_cli.config import load_config

        cfg = load_config().get("browser", {}).get("patchright", {})
        if isinstance(cfg, dict):
            merged.update(cfg)
    except Exception:
        pass

    runtime_proxy = _get_patchright_proxy_override(task_id or "default")
    if runtime_proxy is not None:
        merged["proxy"] = runtime_proxy

    return merged


def _resolve_patchright_cdp_url(cfg: dict[str, Any]) -> str:
    raw = str(cfg.get("cdp_url") or os.environ.get("BROWSER_CDP_URL", "")).strip()
    if not raw:
        return ""
    return resolve_cdp_override(raw)


def _parse_proxy_url(raw_url: str) -> dict[str, Any] | None:
    raw = str(raw_url or "").strip()
    if not raw:
        return None

    candidate = raw if "://" in raw else f"http://{raw}"
    parsed = urlsplit(candidate)
    if not parsed.hostname:
        return None

    server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"

    proxy: dict[str, Any] = {"server": server}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def _resolve_patchright_proxy(cfg: dict[str, Any]) -> dict[str, Any] | None:
    proxy_cfg = cfg.get("proxy")

    if isinstance(proxy_cfg, str):
        return _parse_proxy_url(proxy_cfg)

    if not isinstance(proxy_cfg, dict):
        return None

    resolved: dict[str, Any] = {}

    from_url = _parse_proxy_url(str(proxy_cfg.get("url") or ""))
    if from_url:
        resolved.update(from_url)

    server = str(proxy_cfg.get("server") or "").strip()
    if server:
        # If the server is provided, enforce http:// as the default scheme.
        resolved["server"] = server if "://" in server else f"http://{server}"

    username = proxy_cfg.get("username")
    password = proxy_cfg.get("password")
    bypass = proxy_cfg.get("bypass")

    if username not in (None, ""):
        resolved["username"] = str(username)
    if password not in (None, ""):
        resolved["password"] = str(password)
    if bypass not in (None, ""):
        resolved["bypass"] = str(bypass)

    if not resolved.get("server"):
        return None
    return resolved


def _ensure_xvfb_for_patchright(cfg: dict[str, Any]) -> None:
    """Ensure DISPLAY is available via Xvfb when configured.

    Config keys:
    - browser.patchright.xvfb.enabled (bool)
    - browser.patchright.xvfb.display (default :99)
    - browser.patchright.xvfb.screen (default 1920x1080x24)
    - browser.patchright.xvfb.force (start even if DISPLAY already exists)
    """
    xvfb_cfg = cfg.get("xvfb")
    if not isinstance(xvfb_cfg, dict) or not xvfb_cfg.get("enabled"):
        return

    force = bool(xvfb_cfg.get("force", False))
    if os.environ.get("DISPLAY") and not force:
        return

    display = str(xvfb_cfg.get("display") or ":99").strip() or ":99"
    if not display.startswith(":"):
        display = f":{display}"

    screen = str(xvfb_cfg.get("screen") or "1920x1080x24").strip() or "1920x1080x24"

    with _XVFB_LOCK:
        global _XVFB_PROCESS, _XVFB_DISPLAY

        if _XVFB_PROCESS is not None and _XVFB_PROCESS.poll() is None:
            os.environ["DISPLAY"] = _XVFB_DISPLAY or display
            return

        if shutil.which("Xvfb") is None:
            raise RuntimeError("Patchright xvfb.enabled=true but Xvfb is not installed. Install package 'xvfb'.")

        cmd = ["Xvfb", display, "-screen", "0", screen, "-ac", "+extension", "RANDR"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.25)
        if proc.poll() is not None:
            raise RuntimeError("Failed to start Xvfb for Patchright (process exited immediately).")

        _XVFB_PROCESS = proc
        _XVFB_DISPLAY = display
        os.environ["DISPLAY"] = display


def _timeout_ms(value: Any, default_ms: int) -> int:
    try:
        if value is None:
            return default_ms
        return max(int(value), 1_000)
    except Exception:
        return default_ms


def _action_timeout_ms(cfg: dict[str, Any]) -> int:
    return _timeout_ms(cfg.get("action_timeout_ms"), default_ms=20_000)


def _attach_console_listeners(state: PatchrightRuntimeSession) -> None:
    page = state.page
    if page is None:
        return

    def _on_console(msg: Any) -> None:
        try:
            state.console_messages.append(
                {
                    "type": getattr(msg, "type", lambda: "log")(),
                    "text": getattr(msg, "text", lambda: "")(),
                    "source": "console",
                }
            )
        except Exception:
            pass

    def _on_page_error(err: Any) -> None:
        state.js_errors.append({"message": str(err), "source": "exception"})

    try:
        page.on("console", _on_console)
        page.on("pageerror", _on_page_error)
    except Exception:
        logger.debug("Could not attach console listeners", exc_info=True)


def _iter_frames(page: Any):
    main = getattr(page, "main_frame", None)
    if main is None:
        return

    stack: list[tuple[Any, list[int]]] = [(main, [])]
    while stack:
        frame, path = stack.pop(0)
        yield frame, path
        children = list(getattr(frame, "child_frames", []) or [])
        for idx, child in enumerate(children):
            stack.append((child, [*path, idx]))


def _refs_payload(ref_map: dict[str, ElementRef]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ref, element in ref_map.items():
        out[ref] = {
            "role": element.role,
            "name": element.name,
            "text": element.text,
            "frame_path": element.frame_path or [],
            "selector": element.selector,
            "xpath": element.xpath,
        }
    return out


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_close_patchright_state(state: PatchrightRuntimeSession) -> None:
    cdp_mode = bool(state.metadata.get("cdp_url"))

    if cdp_mode:
        should_close_context = bool(state.metadata.get("cdp_created_context"))
        should_close_page = should_close_context or bool(state.metadata.get("cdp_created_page"))
    else:
        should_close_context = True
        should_close_page = True

    if should_close_page:
        try:
            if state.page is not None:
                state.page.close()
        except Exception:
            pass

    if should_close_context:
        try:
            if state.browser_context is not None:
                state.browser_context.close()
        except Exception:
            pass

    try:
        if state.playwright is not None:
            state.playwright.stop()
    except Exception:
        pass

    if state.user_data_dir:
        try:
            shutil.rmtree(state.user_data_dir, ignore_errors=True)
        except Exception:
            pass


def _inject_annotation_overlays(session: PatchrightRuntimeSession) -> tuple[list[dict[str, Any]], list[str]]:
    annotations: list[dict[str, Any]] = []
    ids: list[str] = []
    for ref, element in session.ref_map.items():
        bbox = element.bbox or {}
        x = float(bbox.get("x", 0.0))
        y = float(bbox.get("y", 0.0))
        width = float(bbox.get("width", 0.0))
        height = float(bbox.get("height", 0.0))
        if width <= 0 or height <= 0:
            continue

        overlay_id = f"hermes-overlay-{ref.lstrip('@')}"
        label = ref.replace("@", "").upper()
        ids.append(overlay_id)
        annotations.append(
            {
                "ref": ref,
                "label": label,
                "role": element.role,
                "name": element.name,
                "frame_path": element.frame_path or [],
            }
        )

        try:
            frame = _frame_for_path(session.page, element.frame_path)
            if frame is None:
                continue
            frame.evaluate(
                """
                (payload) => {
                  const existing = document.getElementById(payload.id);
                  if (existing) existing.remove();
                  const el = document.createElement('div');
                  el.id = payload.id;
                  el.textContent = payload.label;
                  el.style.position = 'fixed';
                  el.style.left = `${payload.x}px`;
                  el.style.top = `${payload.y}px`;
                  el.style.zIndex = '2147483647';
                  el.style.background = 'rgba(255,0,80,0.9)';
                  el.style.color = '#fff';
                  el.style.font = '12px monospace';
                  el.style.padding = '2px 4px';
                  el.style.borderRadius = '3px';
                  el.style.pointerEvents = 'none';
                  document.body.appendChild(el);
                }
                """,
                {
                    "id": overlay_id,
                    "label": label,
                    "x": x,
                    "y": y,
                },
            )
        except Exception:
            continue

    return annotations, ids


def _remove_annotation_overlays(session: PatchrightRuntimeSession, ids: list[str]) -> None:
    if not ids:
        return

    for frame, _ in _iter_frames(session.page):
        try:
            frame.evaluate(
                """
                (payload) => {
                  for (const id of payload.ids) {
                    const node = document.getElementById(id);
                    if (node) node.remove();
                  }
                }
                """,
                {"ids": ids},
            )
        except Exception:
            continue


def _frame_for_path(page: Any, frame_path: list[int] | None):
    frame = getattr(page, "main_frame", None)
    if frame is None:
        return None
    for idx in frame_path or []:
        children = list(getattr(frame, "child_frames", []) or [])
        if idx < 0 or idx >= len(children):
            return None
        frame = children[idx]
    return frame


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
