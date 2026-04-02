"""Tests that browser_navigate SSRF checks respect backend locality and allow_private_urls."""

import json

import pytest

from tools import browser_tool


class _FakeBackend:
    def __init__(self, *, local: bool, final_url: str = "https://example.com"):
        self._local = local
        self.final_url = final_url
        self.calls: list[tuple[str, str]] = []

    def is_local(self) -> bool:
        return self._local

    def navigate(self, task_id: str, url: str) -> dict:
        self.calls.append((task_id, url))
        return {"success": True, "url": self.final_url, "title": "OK"}


class TestPreNavigationSsrf:
    PRIVATE_URL = "http://127.0.0.1:8080/dashboard"

    @pytest.fixture(autouse=True)
    def _setup_common(self, monkeypatch):
        monkeypatch.setattr(browser_tool, "check_website_access", lambda url: None)

    def test_cloud_blocks_private_url_by_default(self, monkeypatch):
        backend = _FakeBackend(local=False)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)
        monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: False)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: False)

        result = json.loads(browser_tool.browser_navigate(self.PRIVATE_URL))

        assert result["success"] is False
        assert "private or internal address" in result["error"]
        assert backend.calls == []

    def test_cloud_allows_private_when_setting_true(self, monkeypatch):
        backend = _FakeBackend(local=False)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)
        monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: True)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: False)

        result = json.loads(browser_tool.browser_navigate(self.PRIVATE_URL))

        assert result["success"] is True
        assert backend.calls[-1][1] == self.PRIVATE_URL

    def test_local_allows_private_url(self, monkeypatch):
        backend = _FakeBackend(local=True)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)
        monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: False)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: False)

        result = json.loads(browser_tool.browser_navigate(self.PRIVATE_URL))

        assert result["success"] is True
        assert backend.calls[-1][1] == self.PRIVATE_URL


class TestPostRedirectSsrf:
    PUBLIC_URL = "https://example.com/redirect"
    PRIVATE_FINAL_URL = "http://192.168.1.1/internal"

    @pytest.fixture(autouse=True)
    def _setup_common(self, monkeypatch):
        monkeypatch.setattr(browser_tool, "check_website_access", lambda url: None)

    def test_cloud_blocks_redirect_to_private(self, monkeypatch):
        backend = _FakeBackend(local=False, final_url=self.PRIVATE_FINAL_URL)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)
        monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: False)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: "192.168" not in url)

        result = json.loads(browser_tool.browser_navigate(self.PUBLIC_URL))

        assert result["success"] is False
        assert "redirect landed on a private/internal address" in result["error"]
        assert backend.calls[-1][1] == "about:blank"

    def test_local_allows_redirect_to_private(self, monkeypatch):
        backend = _FakeBackend(local=True, final_url=self.PRIVATE_FINAL_URL)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)
        monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: False)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: "192.168" not in url)

        result = json.loads(browser_tool.browser_navigate(self.PUBLIC_URL))

        assert result["success"] is True
        assert result["url"] == self.PRIVATE_FINAL_URL
