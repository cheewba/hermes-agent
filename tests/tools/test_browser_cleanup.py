"""Regression tests for browser cleanup orchestration."""

import json


class _FakeSession:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.last_activity = 0.0
        self.metadata = {}


class _FakeBackend:
    def __init__(self, name="fake"):
        self._name = name
        self.closed: list[str] = []
        self.cleaned: list[str] = []
        self.sessions = [_FakeSession("task-1"), _FakeSession("task-2")]

    def backend_name(self):
        return self._name

    def list_sessions(self):
        return list(self.sessions)

    def close_session(self, task_id: str):
        self.closed.append(task_id)
        self.sessions = [s for s in self.sessions if s.task_id != task_id]
        return True

    def emergency_cleanup(self, task_id: str):
        self.cleaned.append(task_id)


class _FakePatchrightBackend(_FakeBackend):
    def __init__(self, *, supports_runtime_proxy: bool = True):
        super().__init__("patchright")
        self.runtime_proxy_calls: list[tuple[str, dict | None]] = []
        self._supports_runtime_proxy = supports_runtime_proxy

    def supports_runtime_proxy(self, task_id: str | None = None):
        return self._supports_runtime_proxy

    def set_runtime_proxy(self, task_id: str, proxy: dict | None):
        self.runtime_proxy_calls.append((task_id, proxy))


class _ResettingPatchrightBackend(_FakePatchrightBackend):
    def __init__(self):
        super().__init__()
        self.current_proxy: dict | None = None

    def set_runtime_proxy(self, task_id: str, proxy: dict | None):
        super().set_runtime_proxy(task_id, proxy)
        self.current_proxy = proxy

    def close_session(self, task_id: str):
        self.current_proxy = None
        return super().close_session(task_id)


class TestScreenshotPathRecovery:
    def test_extracts_standard_absolute_path(self):
        from tools.browser_tool import _extract_screenshot_path_from_text

        assert _extract_screenshot_path_from_text("Screenshot saved to /tmp/foo.png") == "/tmp/foo.png"

    def test_extracts_quoted_absolute_path(self):
        from tools.browser_tool import _extract_screenshot_path_from_text

        assert (
            _extract_screenshot_path_from_text(
                "Screenshot saved to '/Users/david/.hermes/browser_screenshots/shot.png'"
            )
            == "/Users/david/.hermes/browser_screenshots/shot.png"
        )


class TestBrowserCleanup:
    def test_cleanup_browser_closes_task_across_backends(self, monkeypatch):
        from tools import browser_tool

        backend_a = _FakeBackend("a")
        backend_b = _FakeBackend("b")

        monkeypatch.setattr(browser_tool, "get_initialized_backends", lambda: [backend_a, backend_b])
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend_a)

        browser_tool.cleanup_browser("task-1")

        assert "task-1" in backend_a.closed
        assert "task-1" in backend_b.closed

    def test_browser_close_returns_warning_when_session_missing(self, monkeypatch):
        from tools import browser_tool

        backend = _FakeBackend("a")
        backend.sessions = []

        monkeypatch.setattr(browser_tool, "get_initialized_backends", lambda: [backend])
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)

        result = json.loads(browser_tool.browser_close("task-404"))

        assert result["success"] is True
        assert result["closed"] is True
        assert "warning" in result

    def test_emergency_cleanup_calls_backend_hooks(self, monkeypatch):
        from tools import browser_tool

        backend = _FakeBackend("a")

        monkeypatch.setattr(browser_tool, "get_initialized_backends", lambda: [backend])
        browser_tool._cleanup_done = False

        browser_tool._emergency_cleanup_all_sessions()

        assert backend.cleaned == ["task-1", "task-2"]
        assert browser_tool._cleanup_done is True

    def test_browser_set_proxy_applies_runtime_override_and_restarts_session(self, monkeypatch):
        from tools import browser_tool

        backend = _FakePatchrightBackend()
        monkeypatch.setattr(browser_tool, "PatchrightBackend", _FakePatchrightBackend)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)
        monkeypatch.setattr(browser_tool, "get_initialized_backends", lambda: [backend])

        result = json.loads(
            browser_tool.browser_set_proxy(
                proxy_url="http://user:pass@proxy.example:8080",
                task_id="task-proxy",
            )
        )

        assert result["success"] is True
        assert result["session_restarted"] is True
        assert result["proxy"]["server"] == "http://proxy.example:8080"
        assert result["proxy"]["has_auth"] is True
        assert backend.runtime_proxy_calls == [
            ("task-proxy", {"url": "http://user:pass@proxy.example:8080"})
        ]
        assert "task-proxy" in backend.closed

    def test_browser_set_proxy_clear_removes_runtime_override(self, monkeypatch):
        from tools import browser_tool

        backend = _FakePatchrightBackend()
        monkeypatch.setattr(browser_tool, "PatchrightBackend", _FakePatchrightBackend)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)
        monkeypatch.setattr(browser_tool, "get_initialized_backends", lambda: [backend])

        result = json.loads(browser_tool.browser_set_proxy(clear=True, task_id="task-clear"))

        assert result["success"] is True
        assert result["cleared"] is True
        assert backend.runtime_proxy_calls == [("task-clear", None)]

    def test_browser_set_proxy_rejects_non_patchright_backends(self, monkeypatch):
        from tools import browser_tool

        backend = _FakeBackend("agent-browser")
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)

        result = json.loads(browser_tool.browser_set_proxy(proxy_url="http://proxy.example:8080"))

        assert result["success"] is False
        assert "patchright" in result["error"].lower()

    def test_browser_set_proxy_persists_after_session_restart(self, monkeypatch):
        from tools import browser_tool

        backend = _ResettingPatchrightBackend()
        monkeypatch.setattr(browser_tool, "PatchrightBackend", _ResettingPatchrightBackend)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)
        monkeypatch.setattr(browser_tool, "get_initialized_backends", lambda: [backend])

        result = json.loads(browser_tool.browser_set_proxy(proxy_url="http://user:pass@proxy.example:8080", task_id="task-restart"))

        assert result["success"] is True
        assert backend.current_proxy == {"url": "http://user:pass@proxy.example:8080"}
        assert "task-restart" in backend.closed

    def test_browser_set_proxy_rejects_patchright_cdp_mode(self, monkeypatch):
        from tools import browser_tool

        backend = _FakePatchrightBackend(supports_runtime_proxy=False)
        monkeypatch.setattr(browser_tool, "PatchrightBackend", _FakePatchrightBackend)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)
        monkeypatch.setattr(browser_tool, "get_initialized_backends", lambda: [backend])

        result = json.loads(browser_tool.browser_set_proxy(proxy_url="http://proxy.example:8080", task_id="task-cdp"))

        assert result["success"] is False
        assert "cdp" in result["error"].lower()
        assert backend.runtime_proxy_calls == []
