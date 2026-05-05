#!/usr/bin/env python3
"""Browser tool orchestration layer.

Public tool APIs stay stable while execution routes through a pluggable backend:
- agent-browser (default)
- patchright
- camofox
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

try:
    from tools.website_policy import check_website_access
except Exception:
    check_website_access = lambda url: None  # noqa: E731

try:
    from tools.url_safety import is_safe_url as _is_safe_url
except Exception:
    _is_safe_url = lambda url: False  # noqa: E731

from tools.browser_backend_registry import (
    get_backend_by_name,
    get_browser_backend,
    get_initialized_backends,
    load_browser_config,
    resolve_browser_backend_name,
)
from tools.browser_backends.agent_browser import (
    AgentBrowserBackend,
    _SANE_PATH,
    extract_screenshot_path_from_text as _agent_extract_screenshot_path,
    find_agent_browser as _agent_find_agent_browser,
    get_cdp_override as _agent_get_cdp_override,
    resolve_cdp_override as _agent_resolve_cdp_override,
)
from tools.browser_backends.patchright import PatchrightBackend
from tools.browser_snapshot import (
    SNAPSHOT_SUMMARIZE_THRESHOLD,
    extract_relevant_content,
    truncate_snapshot,
)

logger = logging.getLogger(__name__)

# Session inactivity timeout (seconds)
BROWSER_SESSION_INACTIVITY_TIMEOUT = int(os.environ.get("BROWSER_INACTIVITY_TIMEOUT", "300"))

_cleanup_thread: threading.Thread | None = None
_cleanup_running = False
_cleanup_done = False
_cleanup_lock = threading.Lock()

_allow_private_urls_resolved = False
_cached_allow_private_urls = False


BROWSER_TOOL_SCHEMAS = [
    {
        "name": "browser_navigate",
        "description": "Navigate to a URL in the browser. Initializes the session and loads the page. Must be called before other browser tools. For simple information retrieval, prefer web_search or web_extract (faster, cheaper). Use browser tools when you need to interact with a page (click, fill forms, dynamic content).",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to (e.g., 'https://example.com')",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_set_proxy",
        "description": "Set or clear a runtime proxy for the current browser session without writing config files. Primarily for patchright backend. Applying a new proxy closes the current browser session so the next browser_navigate starts with updated network routing.",
        "parameters": {
            "type": "object",
            "properties": {
                "proxy_url": {
                    "type": "string",
                    "description": "Full proxy URL (supports credentials), e.g. 'http://user:pass@host:port'.",
                },
                "server": {
                    "type": "string",
                    "description": "Proxy host:port or scheme://host:port. Optional alternative to proxy_url.",
                },
                "username": {
                    "type": "string",
                    "description": "Proxy username (optional).",
                },
                "password": {
                    "type": "string",
                    "description": "Proxy password (optional).",
                },
                "bypass": {
                    "type": "string",
                    "description": "Comma-separated bypass rules (optional).",
                },
                "clear": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, remove any runtime proxy override for this session and fall back to config.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "browser_solve_cloudflare",
        "description": "Attempt to solve a Cloudflare Turnstile or Managed Challenge on the current page using the 2captcha extension. This helper is only available with the patchright backend and requires a valid 2captcha API key in config. Best-effort only.",
        "parameters": {
            "type": "object",
            "properties": {
                "max_wait_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds to wait for challenge completion before timing out.",
                    "default": 120,
                }
            },
            "required": [],
        },
    },
    {
        "name": "browser_solve_hcaptcha",
        "description": "Attempt to solve an hCaptcha challenge on the current page using hcaptcha-challenger. This helper is only available with the patchright backend and requires the Python package 'hcaptcha-challenger' plus a configured GEMINI_API_KEY. Best-effort only; some challenges may still fail.",
        "parameters": {
            "type": "object",
            "properties": {
                "max_wait_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds to wait for challenge completion before timing out.",
                    "default": 120,
                }
            },
            "required": [],
        },
    },
    {
        "name": "browser_snapshot",
        "description": "Get a text-based snapshot of the current page's accessibility tree. Returns interactive elements with ref IDs (like @e1, @e2) for browser_click and browser_type. full=false (default): compact view with interactive elements. full=true: complete page content. Snapshots over 8000 chars are truncated or LLM-summarized. Requires browser_navigate first.",
        "parameters": {
            "type": "object",
            "properties": {
                "full": {
                    "type": "boolean",
                    "description": "If true, returns complete page content. If false (default), returns compact view with interactive elements only.",
                    "default": False,
                }
            },
            "required": [],
        },
    },
    {
        "name": "browser_click",
        "description": "Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). The ref IDs are shown in square brackets in the snapshot output. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The element reference from the snapshot (e.g., '@e5', '@e12')",
                }
            },
            "required": ["ref"],
        },
    },
    {
        "name": "browser_type",
        "description": "Type text into an input field identified by its ref ID. Clears the field first, then types the new text. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The element reference from the snapshot (e.g., '@e3')",
                },
                "text": {
                    "type": "string",
                    "description": "The text to type into the field",
                },
            },
            "required": ["ref", "text"],
        },
    },
    {
        "name": "browser_scroll",
        "description": "Scroll the page in a direction. Use this to reveal more content that may be below or above the current viewport. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "Direction to scroll",
                }
            },
            "required": ["direction"],
        },
    },
    {
        "name": "browser_back",
        "description": "Navigate back to the previous page in browser history. Requires browser_navigate to be called first.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_press",
        "description": "Press a keyboard key. Useful for submitting forms (Enter), navigating (Tab), or keyboard shortcuts. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown')",
                }
            },
            "required": ["key"],
        },
    },
    {
        "name": "browser_close",
        "description": "Close the browser session and release resources. Call this when done with browser tasks to free up Browserbase session quota.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_get_images",
        "description": "Get a list of all images on the current page with their URLs and alt text. Useful for finding images to analyze with the vision tool. Requires browser_navigate to be called first.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_vision",
        "description": "Take a screenshot of the current page and analyze it with vision AI. Use this when you need to visually understand what's on the page - especially useful for CAPTCHAs, visual verification challenges, complex layouts, or when the text snapshot doesn't capture important visual information. Returns both the AI analysis and a screenshot_path that you can share with the user by including MEDIA:<screenshot_path> in your response. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What you want to know about the page visually. Be specific about what you're looking for.",
                },
                "annotate": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, overlay numbered [N] labels on interactive elements. Each [N] maps to ref @eN for subsequent browser commands. Useful for QA and spatial reasoning about page layout.",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "browser_console",
        "description": "Get browser console output and JavaScript errors from the current page. Returns console.log/warn/error/info messages and uncaught JS exceptions. Use this to detect silent JavaScript errors, failed API calls, and application warnings. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "clear": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, clear the message buffers after reading",
                }
            },
            "required": [],
        },
    },
]


def _effective_task_id(task_id: Optional[str]) -> str:
    return task_id or "default"


def _get_backend():
    return get_browser_backend(load_browser_config())


def _normalize_result(result: Any, default_error: str) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    return {"success": False, "error": default_error}


def _redact_proxy_target(proxy_target: str) -> str:
    raw = str(proxy_target or "").strip()
    if not raw:
        return ""

    # Bare host:port values have no userinfo component to redact.
    if "://" not in raw:
        return raw

    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw

    host = parsed.hostname or ""
    if not host:
        return raw

    netloc = host
    if parsed.port is not None:
        netloc = f"{host}:{parsed.port}"

    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _allow_private_urls() -> bool:
    global _allow_private_urls_resolved, _cached_allow_private_urls
    if _allow_private_urls_resolved:
        return _cached_allow_private_urls

    _allow_private_urls_resolved = True
    cfg = load_browser_config()
    _cached_allow_private_urls = bool(cfg.get("allow_private_urls"))
    return _cached_allow_private_urls


def _cleanup_inactive_browser_sessions() -> None:
    cutoff = time.time() - BROWSER_SESSION_INACTIVITY_TIMEOUT
    for backend in get_initialized_backends():
        try:
            for session in backend.list_sessions():
                if session.last_activity < cutoff:
                    logger.info(
                        "Cleaning inactive browser session backend=%s task=%s",
                        backend.backend_name(),
                        session.task_id,
                    )
                    backend.close_session(session.task_id)
        except Exception:
            logger.warning("Error cleaning inactive sessions for backend=%s", backend.backend_name(), exc_info=True)


def _browser_cleanup_thread_worker() -> None:
    global _cleanup_running
    while _cleanup_running:
        try:
            _cleanup_inactive_browser_sessions()
        except Exception:
            logger.warning("Browser cleanup thread error", exc_info=True)

        for _ in range(30):
            if not _cleanup_running:
                break
            time.sleep(1)


def _start_browser_cleanup_thread() -> None:
    global _cleanup_thread, _cleanup_running
    with _cleanup_lock:
        if _cleanup_thread is None or not _cleanup_thread.is_alive():
            _cleanup_running = True
            _cleanup_thread = threading.Thread(
                target=_browser_cleanup_thread_worker,
                daemon=True,
                name="browser-cleanup",
            )
            _cleanup_thread.start()


def _stop_browser_cleanup_thread() -> None:
    global _cleanup_running
    _cleanup_running = False
    if _cleanup_thread is not None:
        _cleanup_thread.join(timeout=5)


def _emergency_cleanup_all_sessions() -> None:
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    for backend in get_initialized_backends():
        for session in backend.list_sessions():
            try:
                backend.emergency_cleanup(session.task_id)
            except Exception:
                logger.warning(
                    "Emergency cleanup failed backend=%s task=%s",
                    backend.backend_name(),
                    session.task_id,
                    exc_info=True,
                )


atexit.register(_emergency_cleanup_all_sessions)
atexit.register(_stop_browser_cleanup_thread)


def browser_navigate(url: str, task_id: Optional[str] = None) -> str:
    from agent.redact import _PREFIX_RE

    if _PREFIX_RE.search(url):
        return json.dumps(
            {
                "success": False,
                "error": "Blocked: URL contains what appears to be an API key or token. Secrets must not be sent in URLs.",
            }
        )

    backend = _get_backend()

    if not backend.is_local() and not _allow_private_urls() and not _is_safe_url(url):
        return json.dumps(
            {
                "success": False,
                "error": "Blocked: URL targets a private or internal address",
            }
        )

    blocked = check_website_access(url)
    if blocked:
        return json.dumps(
            {
                "success": False,
                "error": blocked["message"],
                "blocked_by_policy": {
                    "host": blocked["host"],
                    "rule": blocked["rule"],
                    "source": blocked["source"],
                },
            }
        )

    _start_browser_cleanup_thread()
    task = _effective_task_id(task_id)
    result = _normalize_result(backend.navigate(task, url), "Navigation failed")

    if result.get("success") and not backend.is_local() and not _allow_private_urls():
        final_url = result.get("url", "")
        if final_url and final_url != url and not _is_safe_url(final_url):
            try:
                backend.navigate(task, "about:blank")
            except Exception:
                pass
            return json.dumps(
                {
                    "success": False,
                    "error": "Blocked: redirect landed on a private/internal address",
                },
                ensure_ascii=False,
            )

    return json.dumps(result, ensure_ascii=False)


def browser_set_proxy(
    proxy_url: str = "",
    server: str = "",
    username: str = "",
    password: str = "",
    bypass: str = "",
    clear: bool = False,
    task_id: Optional[str] = None,
) -> str:
    backend = _get_backend()
    if not isinstance(backend, PatchrightBackend):
        return json.dumps(
            {
                "success": False,
                "error": "Runtime proxy override is currently supported only by the patchright browser backend.",
                "backend": backend.backend_name(),
            },
            ensure_ascii=False,
        )

    task = _effective_task_id(task_id)

    supports_runtime_proxy = getattr(backend, "supports_runtime_proxy", None)
    if callable(supports_runtime_proxy) and not supports_runtime_proxy(task):
        return json.dumps(
            {
                "success": False,
                "error": "Runtime proxy override is unavailable when patchright is attached via CDP. Configure proxy on the remote browser host or disable browser.patchright.cdp_url.",
                "backend": backend.backend_name(),
            },
            ensure_ascii=False,
        )

    if clear:
        backend.set_runtime_proxy(task, None)
        cleanup_browser(task)
        return json.dumps(
            {
                "success": True,
                "cleared": True,
                "session_restarted": True,
                "message": "Runtime proxy cleared for this session; next navigation will use config/default routing.",
            },
            ensure_ascii=False,
        )

    proxy_payload: dict[str, str] = {}
    if proxy_url:
        proxy_payload["url"] = proxy_url
    if server:
        proxy_payload["server"] = server
    if username:
        proxy_payload["username"] = username
    if password:
        proxy_payload["password"] = password
    if bypass:
        proxy_payload["bypass"] = bypass

    if not proxy_payload:
        return json.dumps(
            {
                "success": False,
                "error": "Provide proxy_url or server (with optional username/password), or set clear=true.",
            },
            ensure_ascii=False,
        )

    # Restart current browser session first, then persist the new runtime override
    # for the replacement session.
    cleanup_browser(task)
    backend.set_runtime_proxy(task, proxy_payload)

    proxy_target = proxy_payload.get("server") or proxy_payload.get("url", "")
    parsed_url = None
    if "://" in proxy_payload.get("url", ""):
        try:
            parsed_url = urlsplit(proxy_payload.get("url", ""))
        except Exception:
            parsed_url = None

    return json.dumps(
        {
            "success": True,
            "applied": True,
            "session_restarted": True,
            "proxy": {
                "server": _redact_proxy_target(proxy_target),
                "has_auth": bool(
                    proxy_payload.get("username")
                    or proxy_payload.get("password")
                    or (parsed_url and (parsed_url.username or parsed_url.password))
                ),
                "bypass": proxy_payload.get("bypass", ""),
            },
        },
        ensure_ascii=False,
    )


def browser_solve_cloudflare(max_wait_seconds: int = 120, task_id: Optional[str] = None) -> str:
    backend = _get_backend()
    if not hasattr(backend, "solve_cloudflare"):
        return json.dumps(
            {
                "success": False,
                "error": f"The browser_solve_cloudflare tool is not supported by backend '{backend.backend_name()}'.",
            },
            ensure_ascii=False,
        )

    task = _effective_task_id(task_id)
    _start_browser_cleanup_thread()
    
    result = backend.solve_cloudflare(task, max_wait_seconds=max_wait_seconds)
    return json.dumps(result, ensure_ascii=False)

def browser_solve_hcaptcha(max_wait_seconds: int = 120, task_id: Optional[str] = None) -> str:
    backend = _get_backend()
    task = _effective_task_id(task_id)

    if not isinstance(backend, PatchrightBackend):
        return json.dumps(
            {
                "success": False,
                "error": "hCaptcha solver is currently supported only by the patchright browser backend.",
            },
            ensure_ascii=False,
        )

    if not backend.supports_hcaptcha_challenger():
        return json.dumps(
            {
                "success": False,
                "error": (
                    "hcaptcha-challenger support is unavailable. Install Python package "
                    "'hcaptcha-challenger' and configure GEMINI_API_KEY, then retry."
                ),
            },
            ensure_ascii=False,
        )

    timeout = max(10, int(max_wait_seconds or 120))
    result = _normalize_result(
        backend.solve_hcaptcha(task, max_wait_seconds=timeout),
        "hCaptcha solve failed",
    )

    return json.dumps(result, ensure_ascii=False)


def browser_snapshot(
    full: bool = False,
    task_id: Optional[str] = None,
    user_task: Optional[str] = None,
) -> str:
    backend = _get_backend()
    _start_browser_cleanup_thread()

    result = _normalize_result(backend.snapshot(_effective_task_id(task_id), full=full), "Failed to get snapshot")
    if not result.get("success"):
        return json.dumps(
            {
                "success": False,
                "error": result.get("error", "Failed to get snapshot"),
            },
            ensure_ascii=False,
        )

    snapshot_text = str(result.get("snapshot", ""))
    if len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD and user_task:
        snapshot_text = extract_relevant_content(snapshot_text, user_task)
    elif len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD:
        snapshot_text = truncate_snapshot(snapshot_text)

    refs = result.get("refs", {})
    element_count = result.get("element_count")
    if element_count is None and isinstance(refs, dict):
        element_count = len(refs)

    return json.dumps(
        {
            "success": True,
            "snapshot": snapshot_text,
            "element_count": int(element_count or 0),
        },
        ensure_ascii=False,
    )


def browser_click(ref: str, task_id: Optional[str] = None) -> str:
    backend = _get_backend()
    _start_browser_cleanup_thread()
    result = _normalize_result(backend.click(_effective_task_id(task_id), ref), "Click failed")
    return json.dumps(result, ensure_ascii=False)


def browser_type(ref: str, text: str, task_id: Optional[str] = None) -> str:
    backend = _get_backend()
    _start_browser_cleanup_thread()
    result = _normalize_result(backend.type(_effective_task_id(task_id), ref, text), "Type failed")
    return json.dumps(result, ensure_ascii=False)


def browser_scroll(direction: str, task_id: Optional[str] = None) -> str:
    if direction not in ["up", "down"]:
        return json.dumps(
            {
                "success": False,
                "error": f"Invalid direction '{direction}'. Use 'up' or 'down'.",
            },
            ensure_ascii=False,
        )

    backend = _get_backend()
    _start_browser_cleanup_thread()
    result = _normalize_result(backend.scroll(_effective_task_id(task_id), direction), "Scroll failed")
    return json.dumps(result, ensure_ascii=False)


def browser_back(task_id: Optional[str] = None) -> str:
    backend = _get_backend()
    _start_browser_cleanup_thread()
    result = _normalize_result(backend.back(_effective_task_id(task_id)), "Back failed")
    return json.dumps(result, ensure_ascii=False)


def browser_press(key: str, task_id: Optional[str] = None) -> str:
    backend = _get_backend()
    _start_browser_cleanup_thread()
    result = _normalize_result(backend.press(_effective_task_id(task_id), key), "Press failed")
    return json.dumps(result, ensure_ascii=False)


def browser_close(task_id: Optional[str] = None) -> str:
    task = _effective_task_id(task_id)
    had_session = any(task in [s.task_id for s in backend.list_sessions()] for backend in get_initialized_backends())

    cleanup_browser(task)

    response = {"success": True, "closed": True}
    if not had_session:
        response["warning"] = "Session may not have been active"
    return json.dumps(response, ensure_ascii=False)


def browser_console(clear: bool = False, task_id: Optional[str] = None) -> str:
    backend = _get_backend()
    _start_browser_cleanup_thread()
    result = _normalize_result(
        backend.console(_effective_task_id(task_id), clear=clear),
        "Failed to capture browser console",
    )
    return json.dumps(result, ensure_ascii=False)


def browser_get_images(task_id: Optional[str] = None) -> str:
    backend = _get_backend()
    _start_browser_cleanup_thread()
    result = _normalize_result(backend.get_images(_effective_task_id(task_id)), "Failed to get images")
    return json.dumps(result, ensure_ascii=False)


def browser_vision(question: str, annotate: bool = False, task_id: Optional[str] = None) -> str:
    backend = _get_backend()
    _start_browser_cleanup_thread()
    result = _normalize_result(
        backend.vision(_effective_task_id(task_id), question=question, annotate=annotate),
        "Vision analysis failed",
    )
    return json.dumps(result, ensure_ascii=False)


def cleanup_browser(task_id: Optional[str] = None) -> None:
    task = _effective_task_id(task_id)
    backend = _get_backend()

    # Close task in all initialized backends first.
    for b in get_initialized_backends():
        try:
            b.close_session(task)
        except Exception:
            logger.debug("Failed to close browser session backend=%s task=%s", b.backend_name(), task, exc_info=True)

    # Ensure active backend is also attempted.
    try:
        backend.close_session(task)
    except Exception:
        logger.debug("Failed to close active backend session task=%s", task, exc_info=True)


def cleanup_all_browsers() -> None:
    backend = _get_backend()
    backends = {b.backend_name(): b for b in get_initialized_backends()}
    backends[backend.backend_name()] = backend

    for b in backends.values():
        for session in b.list_sessions():
            try:
                b.close_session(session.task_id)
            except Exception:
                logger.debug("Failed to close session backend=%s task=%s", b.backend_name(), session.task_id, exc_info=True)


def get_active_browser_sessions() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    backend = _get_backend()
    backends = {b.backend_name(): b for b in get_initialized_backends()}
    backends[backend.backend_name()] = backend

    for b in backends.values():
        for session in b.list_sessions():
            payload = dict(session.metadata)
            payload["backend"] = b.backend_name()
            payload.setdefault("current_url", session.current_url)
            out[session.task_id] = payload
    return out


def check_browser_requirements() -> bool:
    try:
        backend = _get_backend()
    except Exception:
        return False
    return backend.is_configured()


def check_browser_set_proxy_requirements() -> bool:
    try:
        backend = _get_backend()
    except Exception:
        return False

    if not isinstance(backend, PatchrightBackend):
        return False

    return backend.is_configured()


def check_browser_solve_hcaptcha_requirements() -> bool:
    try:
        backend = _get_backend()
    except Exception:
        return False

    if not isinstance(backend, PatchrightBackend):
        return False

    return backend.is_configured() and backend.supports_hcaptcha_challenger()


# ---------------------------------------------------------------------------
# Compatibility wrappers retained for testability and external imports.
# These are thin delegates to AgentBrowserBackend helpers.
# ---------------------------------------------------------------------------

def _is_camofox_mode() -> bool:
    return resolve_browser_backend_name(load_browser_config()) == "camofox"


def _is_local_backend() -> bool:
    try:
        return _get_backend().is_local()
    except Exception:
        return False


def _discover_homebrew_node_dirs() -> list[str]:
    return AgentBrowserBackend.discover_homebrew_node_dirs()


def _find_agent_browser() -> str:
    return _agent_find_agent_browser()


def _resolve_cdp_override(cdp_url: str) -> str:
    return _agent_resolve_cdp_override(cdp_url)


def _get_cdp_override() -> str:
    return _agent_get_cdp_override()


def _extract_screenshot_path_from_text(text: str) -> str | None:
    return _agent_extract_screenshot_path(text)


_last_screenshot_cleanup_by_dir: dict[str, float] = {}


def _cleanup_old_screenshots(screenshots_dir: str | Path, max_age_hours: int = 24) -> None:
    """Compatibility helper used by gateway tests.

    Cleans browser_screenshot_*.png files older than max_age_hours.
    Throttled to at most once per directory per hour.
    """
    try:
        dir_path = Path(screenshots_dir)
        if not dir_path.exists() or not dir_path.is_dir():
            return

        key = str(dir_path.resolve())
        now = time.time()
        last = _last_screenshot_cleanup_by_dir.get(key, 0)
        if now - last < 3600:
            return
        _last_screenshot_cleanup_by_dir[key] = now

        cutoff = now - (max_age_hours * 3600)
        for file_path in dir_path.glob("browser_screenshot_*.png"):
            try:
                if file_path.stat().st_mtime < cutoff:
                    file_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to cleanup screenshot %s", file_path, exc_info=True)
    except Exception:
        logger.debug("Failed to cleanup screenshot directory %s", screenshots_dir, exc_info=True)


def _get_agent_backend() -> AgentBrowserBackend:
    backend = get_backend_by_name("agent-browser")
    if not isinstance(backend, AgentBrowserBackend):
        raise RuntimeError("agent-browser backend is unavailable")
    return backend


def _get_session_info(task_id: Optional[str] = None) -> dict[str, Any]:
    return _get_agent_backend().get_session_info(_effective_task_id(task_id))


def _run_browser_command(
    task_id: str,
    command: str,
    args: list[str] | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    return _get_agent_backend().run_command(task_id, command, args or [], timeout=timeout)


# Backward-compatible names imported by tests and legacy modules
_extract_relevant_content = extract_relevant_content
_truncate_snapshot = truncate_snapshot


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry

_BROWSER_SCHEMA_MAP = {s["name"]: s for s in BROWSER_TOOL_SCHEMAS}

registry.register(
    name="browser_navigate",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_navigate"],
    handler=lambda args, **kw: browser_navigate(url=args.get("url", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🌐",
)
registry.register(
    name="browser_set_proxy",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_set_proxy"],
    handler=lambda args, **kw: browser_set_proxy(
        proxy_url=args.get("proxy_url", ""),
        server=args.get("server", ""),
        username=args.get("username", ""),
        password=args.get("password", ""),
        bypass=args.get("bypass", ""),
        clear=args.get("clear", False),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_browser_set_proxy_requirements,
    emoji="🛡️",
)
registry.register(
    name="browser_solve_cloudflare",
    toolset="browser",
    schema=next(s for s in BROWSER_TOOL_SCHEMAS if s["name"] == "browser_solve_cloudflare"),
    handler=lambda args, **kwargs: browser_solve_cloudflare(
        max_wait_seconds=args.get("max_wait_seconds", 120),
        task_id=kwargs.get("task_id"),
    ),
    check_fn=check_browser_solve_hcaptcha_requirements,
)

registry.register(
    name="browser_solve_hcaptcha",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_solve_hcaptcha"],
    handler=lambda args, **kw: browser_solve_hcaptcha(
        max_wait_seconds=args.get("max_wait_seconds", 120),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_browser_solve_hcaptcha_requirements,
    emoji="🧩",
)
registry.register(
    name="browser_snapshot",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_snapshot"],
    handler=lambda args, **kw: browser_snapshot(
        full=args.get("full", False), task_id=kw.get("task_id"), user_task=kw.get("user_task")
    ),
    check_fn=check_browser_requirements,
    emoji="📸",
)
registry.register(
    name="browser_click",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_click"],
    handler=lambda args, **kw: browser_click(ref=args.get("ref", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="👆",
)
registry.register(
    name="browser_type",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_type"],
    handler=lambda args, **kw: browser_type(
        ref=args.get("ref", ""), text=args.get("text", ""), task_id=kw.get("task_id")
    ),
    check_fn=check_browser_requirements,
    emoji="⌨️",
)
registry.register(
    name="browser_scroll",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_scroll"],
    handler=lambda args, **kw: browser_scroll(direction=args.get("direction", "down"), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="📜",
)
registry.register(
    name="browser_back",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_back"],
    handler=lambda args, **kw: browser_back(task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="◀️",
)
registry.register(
    name="browser_press",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_press"],
    handler=lambda args, **kw: browser_press(key=args.get("key", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="⌨️",
)
registry.register(
    name="browser_close",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_close"],
    handler=lambda args, **kw: browser_close(task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🚪",
)
registry.register(
    name="browser_get_images",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_get_images"],
    handler=lambda args, **kw: browser_get_images(task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🖼️",
)
registry.register(
    name="browser_vision",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_vision"],
    handler=lambda args, **kw: browser_vision(
        question=args.get("question", ""), annotate=args.get("annotate", False), task_id=kw.get("task_id")
    ),
    check_fn=check_browser_requirements,
    emoji="👁️",
)
registry.register(
    name="browser_console",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_console"],
    handler=lambda args, **kw: browser_console(clear=args.get("clear", False), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🖥️",
)
