from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from tools.browser_backend_base import BrowserBackend, BrowserSessionState
from tools.browser_camofox import (
    camofox_back,
    camofox_click,
    camofox_close,
    camofox_console,
    camofox_get_images,
    camofox_navigate,
    camofox_press,
    camofox_scroll,
    camofox_snapshot,
    camofox_type,
    camofox_vision,
    check_camofox_available,
    cleanup_all_camofox_sessions,
)
from tools.browser_session_store import BrowserSessionStore

logger = logging.getLogger(__name__)


class CamofoxBackend(BrowserBackend):
    """Browser backend adapter for tools/browser_camofox.py."""

    def __init__(self) -> None:
        self._sessions: BrowserSessionStore[BrowserSessionState] = BrowserSessionStore()

    def backend_name(self) -> str:
        return "camofox"

    def is_configured(self) -> bool:
        # Requires server URL, but avoid network calls here (tool availability checks must be cheap).
        return bool(os.getenv("CAMOFOX_URL", "").strip())

    def is_local(self) -> bool:
        # Camofox executes navigation on the configured CAMOFOX_URL service,
        # so treat it as remote to keep SSRF private-address guards enabled.
        return False

    def init_session(self, task_id: str) -> BrowserSessionState:
        task_id = task_id or "default"

        def _factory() -> BrowserSessionState:
            now = time.time()
            return BrowserSessionState(task_id=task_id, started_at=now, last_activity=now)

        return self._sessions.get_or_create(task_id, _factory)

    def get_session(self, task_id: str) -> BrowserSessionState | None:
        return self._sessions.get(task_id)

    def list_sessions(self) -> list[BrowserSessionState]:
        return self._sessions.values()

    def close_session(self, task_id: str) -> bool:
        task_id = task_id or "default"
        self._sessions.remove(task_id)
        result = _decode(camofox_close(task_id))
        return bool(result.get("success", False))

    def emergency_cleanup(self, task_id: str) -> None:
        try:
            self.close_session(task_id)
        except Exception:
            pass

    def navigate(self, task_id: str, url: str) -> dict[str, Any]:
        self.init_session(task_id)
        result = _decode(camofox_navigate(url, task_id))
        self._touch(task_id)
        return result

    def snapshot(self, task_id: str, full: bool = False) -> dict[str, Any]:
        self.init_session(task_id)
        result = _decode(camofox_snapshot(full=full, task_id=task_id))
        self._touch(task_id)
        return result

    def click(self, task_id: str, ref: str) -> dict[str, Any]:
        self.init_session(task_id)
        result = _decode(camofox_click(ref, task_id))
        self._touch(task_id)
        return result

    def type(self, task_id: str, ref: str, text: str) -> dict[str, Any]:
        self.init_session(task_id)
        result = _decode(camofox_type(ref, text, task_id))
        self._touch(task_id)
        return result

    def scroll(self, task_id: str, direction: str) -> dict[str, Any]:
        self.init_session(task_id)
        result = _decode(camofox_scroll(direction, task_id))
        self._touch(task_id)
        return result

    def press(self, task_id: str, key: str) -> dict[str, Any]:
        self.init_session(task_id)
        result = _decode(camofox_press(key, task_id))
        self._touch(task_id)
        return result

    def back(self, task_id: str) -> dict[str, Any]:
        self.init_session(task_id)
        result = _decode(camofox_back(task_id))
        self._touch(task_id)
        return result

    def get_images(self, task_id: str) -> dict[str, Any]:
        self.init_session(task_id)
        result = _decode(camofox_get_images(task_id))
        self._touch(task_id)
        return result

    def vision(self, task_id: str, question: str, annotate: bool = False) -> dict[str, Any]:
        self.init_session(task_id)
        result = _decode(camofox_vision(question, annotate, task_id))
        self._touch(task_id)
        return result

    def console(self, task_id: str, clear: bool = False) -> dict[str, Any]:
        self.init_session(task_id)
        result = _decode(camofox_console(clear, task_id))
        self._touch(task_id)
        return result

    def _touch(self, task_id: str) -> None:
        self._sessions.touch(task_id)


# Convenience exports for orchestration layer

def check_available() -> bool:
    return check_camofox_available()


def cleanup_all() -> None:
    cleanup_all_camofox_sessions()


def _decode(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
        if isinstance(value, dict):
            return value
    except Exception:
        logger.debug("Invalid JSON from camofox backend", exc_info=True)
    return {"success": False, "error": "Invalid response from camofox backend"}

    def solve_cloudflare(self, task_id: str, max_wait_seconds: int = 120) -> dict[str, Any]:
        return {"success": False, "error": "Not supported on this backend"}
