import os

from tools.browser_backend_registry import (
    _BACKEND_CLASSES,
    _BACKEND_INSTANCES,
    available_backend_names,
    get_browser_backend,
    resolve_browser_backend_name,
    reset_backend_registry_for_tests,
)


def setup_function():
    reset_backend_registry_for_tests()


def teardown_function():
    reset_backend_registry_for_tests()


def test_backend_resolution_prefers_explicit_config_over_env(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    cfg = {"backend": "agent-browser"}
    assert resolve_browser_backend_name(cfg) == "agent-browser"


def test_backend_resolution_uses_camofox_for_empty_explicit_backend(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    assert resolve_browser_backend_name({"backend": ""}) == "camofox"


def test_backend_resolution_uses_camofox_when_env_set_and_no_explicit(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    assert resolve_browser_backend_name({}) == "camofox"


def test_backend_resolution_defaults_to_agent_browser(monkeypatch):
    monkeypatch.delenv("CAMOFOX_URL", raising=False)
    assert resolve_browser_backend_name({}) == "agent-browser"


def test_get_backend_falls_back_for_unknown_backend(monkeypatch):
    monkeypatch.delenv("CAMOFOX_URL", raising=False)
    backend = get_browser_backend({"backend": "totally-unknown"})
    assert backend.backend_name() == "agent-browser"


def test_get_backend_preserves_explicit_unconfigured_backend(monkeypatch):
    monkeypatch.delenv("CAMOFOX_URL", raising=False)

    class _UnconfiguredPatchright:
        def backend_name(self):
            return "patchright"

        def is_configured(self):
            return False

    original = _BACKEND_CLASSES["patchright"]
    _BACKEND_CLASSES["patchright"] = _UnconfiguredPatchright
    _BACKEND_INSTANCES.pop("patchright", None)
    try:
        backend = get_browser_backend({"backend": "patchright"})
        assert backend.backend_name() == "patchright"
        assert backend.is_configured() is False
    finally:
        _BACKEND_CLASSES["patchright"] = original
        reset_backend_registry_for_tests()


def test_available_backend_names_hides_unconfigured_patchright(monkeypatch):
    monkeypatch.delenv("CAMOFOX_URL", raising=False)

    class _UnconfiguredPatchright:
        def backend_name(self):
            return "patchright"

        def is_configured(self):
            return False

    original = _BACKEND_CLASSES["patchright"]
    _BACKEND_CLASSES["patchright"] = _UnconfiguredPatchright
    _BACKEND_INSTANCES.pop("patchright", None)
    try:
        names = available_backend_names()
        assert "patchright" not in names
        assert "agent-browser" in names
    finally:
        _BACKEND_CLASSES["patchright"] = original
        reset_backend_registry_for_tests()
