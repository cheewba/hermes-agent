import os

from tools.browser_backends.camofox import CamofoxBackend


def test_camofox_is_treated_as_remote_for_ssrf_guards(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "https://camofox.example")
    backend = CamofoxBackend()

    assert backend.is_configured() is True
    assert backend.is_local() is False


def test_camofox_not_configured_without_url(monkeypatch):
    monkeypatch.delenv("CAMOFOX_URL", raising=False)
    backend = CamofoxBackend()

    assert backend.is_configured() is False
    assert backend.is_local() is False
