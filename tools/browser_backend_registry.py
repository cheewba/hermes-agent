from __future__ import annotations

import logging
import os
from typing import Any

from hermes_constants import get_hermes_home
from tools.browser_backend_base import BrowserBackend
from tools.browser_backends.agent_browser import AgentBrowserBackend
from tools.browser_backends.camofox import CamofoxBackend
from tools.browser_backends.patchright import PatchrightBackend

logger = logging.getLogger(__name__)


_BACKEND_CLASSES: dict[str, type[BrowserBackend]] = {
    "agent-browser": AgentBrowserBackend,
    "patchright": PatchrightBackend,
    "camofox": CamofoxBackend,
}

_BACKEND_INSTANCES: dict[str, BrowserBackend] = {}


def _get_or_create_backend_instance(name: str, backend_cls: type[BrowserBackend]) -> BrowserBackend:
    backend = _BACKEND_INSTANCES.get(name)
    if backend is None:
        backend = backend_cls()
        _BACKEND_INSTANCES[name] = backend
    return backend


def load_browser_config() -> dict[str, Any]:
    """Load raw browser config from config.yaml (without merged defaults)."""
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
            logger.debug("Could not read browser config from %s", config_path, exc_info=True)

    return {}


def resolve_browser_backend_name(config: dict[str, Any] | None = None) -> str:
    """Resolve active browser backend from config/env.

    Priority:
    1) browser.backend explicit config
    2) CAMOFOX_URL env fallback
    3) agent-browser default
    """

    cfg = config if config is not None else load_browser_config()
    explicit = (cfg.get("backend") if isinstance(cfg, dict) else None) or ""
    explicit = str(explicit).strip()
    if explicit:
        return explicit

    if os.getenv("CAMOFOX_URL", "").strip():
        return "camofox"

    return "agent-browser"


def get_browser_backend(config: dict[str, Any] | None = None) -> BrowserBackend:
    name = resolve_browser_backend_name(config)
    backend_cls = _BACKEND_CLASSES.get(name)
    if backend_cls is None:
        logger.warning(
            "Unknown browser backend '%s'; falling back to agent-browser (supported: %s)",
            name,
            ", ".join(sorted(_BACKEND_CLASSES)),
        )
        name = "agent-browser"
        backend_cls = _BACKEND_CLASSES[name]

    return _get_or_create_backend_instance(name, backend_cls)


def get_backend_by_name(name: str) -> BrowserBackend | None:
    name = (name or "").strip()
    backend_cls = _BACKEND_CLASSES.get(name)
    if backend_cls is None:
        return None

    return _get_or_create_backend_instance(name, backend_cls)


def get_initialized_backends() -> list[BrowserBackend]:
    return list(_BACKEND_INSTANCES.values())


def available_backend_names() -> list[str]:
    available: list[str] = []
    for name, backend_cls in sorted(_BACKEND_CLASSES.items()):
        backend = _get_or_create_backend_instance(name, backend_cls)
        if backend.is_configured() or name == "agent-browser":
            available.append(name)
    return available


def reset_backend_registry_for_tests() -> None:
    _BACKEND_INSTANCES.clear()
