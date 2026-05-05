import json


class _FakePatchrightBackend:
    def __init__(self, *, supported: bool = True):
        self._supported = supported
        self.calls: list[tuple[str, int]] = []

    def is_configured(self):
        return True

    def supports_hcaptcha_challenger(self):
        return self._supported

    def solve_hcaptcha(self, task_id: str, max_wait_seconds: int = 120):
        self.calls.append((task_id, max_wait_seconds))
        return {"success": True, "solved": True, "backend": "patchright"}


class _FakeNonPatchrightBackend:
    def is_configured(self):
        return True


class TestBrowserSolveHCaptchaTool:
    def test_rejects_non_patchright_backends(self, monkeypatch):
        from tools import browser_tool

        monkeypatch.setattr(browser_tool, "_get_backend", lambda: _FakeNonPatchrightBackend())

        result = json.loads(browser_tool.browser_solve_hcaptcha())
        assert result["success"] is False
        assert "patchright" in result["error"].lower()

    def test_rejects_when_challenger_not_available(self, monkeypatch):
        from tools import browser_tool

        backend = _FakePatchrightBackend(supported=False)
        monkeypatch.setattr(browser_tool, "PatchrightBackend", _FakePatchrightBackend)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)

        result = json.loads(browser_tool.browser_solve_hcaptcha())
        assert result["success"] is False
        assert "hcaptcha-challenger" in result["error"].lower()

    def test_calls_patchright_backend_solver(self, monkeypatch):
        from tools import browser_tool

        backend = _FakePatchrightBackend(supported=True)
        monkeypatch.setattr(browser_tool, "PatchrightBackend", _FakePatchrightBackend)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)

        result = json.loads(browser_tool.browser_solve_hcaptcha(max_wait_seconds=45, task_id="task-h"))
        assert result["success"] is True
        assert backend.calls == [("task-h", 45)]

    def test_check_fn_blocks_non_patchright(self, monkeypatch):
        from tools import browser_tool

        monkeypatch.setattr(browser_tool, "_get_backend", lambda: _FakeNonPatchrightBackend())
        assert browser_tool.check_browser_solve_hcaptcha_requirements() is False

    def test_check_fn_requires_module_support(self, monkeypatch):
        from tools import browser_tool

        backend = _FakePatchrightBackend(supported=False)
        monkeypatch.setattr(browser_tool, "PatchrightBackend", _FakePatchrightBackend)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend)
        assert browser_tool.check_browser_solve_hcaptcha_requirements() is False

        backend2 = _FakePatchrightBackend(supported=True)
        monkeypatch.setattr(browser_tool, "_get_backend", lambda: backend2)
        assert browser_tool.check_browser_solve_hcaptcha_requirements() is True
