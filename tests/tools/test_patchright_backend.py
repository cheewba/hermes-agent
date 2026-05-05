import importlib.util
import time

import pytest

from tools.browser_backend_base import ElementRef
from tools.browser_backends import patchright as patchright_mod
from tools.browser_backends.patchright import PatchrightBackend, PatchrightRuntimeSession


class _FakeLocator:
    def __init__(self):
        self.clicked = False
        self.filled = None

    def scroll_into_view_if_needed(self, timeout=None):
        return None

    def click(self, timeout=None):
        self.clicked = True

    def fill(self, text, timeout=None):
        self.filled = text


class _FakeFrame:
    def __init__(self, elements=None, child_frames=None, page_text="Full page text"):
        self._elements = elements or []
        self.child_frames = child_frames or []
        self._page_text = page_text

    def evaluate(self, script, payload):
        include_text = bool(payload.get("includeText"))
        return {
            "elements": list(self._elements),
            "page_text": self._page_text if include_text else "",
        }


class _FakePage:
    def __init__(self, main_frame):
        self.main_frame = main_frame
        self.url = "https://example.com"
        self._title = "Example"

    def title(self):
        return self._title


def _session(task_id: str, page) -> PatchrightRuntimeSession:
    now = time.time()
    return PatchrightRuntimeSession(
        task_id=task_id,
        started_at=now,
        last_activity=now,
        page=page,
        browser_context=object(),
        playwright=object(),
    )


def test_stale_ref_returns_structured_error(monkeypatch):
    backend = PatchrightBackend()
    page = _FakePage(_FakeFrame())
    session = _session("task", page)
    session.ref_map = {"@e1": ElementRef(ref="@e1", selector="button")}
    backend._sessions.set("task", session)

    monkeypatch.setattr("tools.browser_backends.patchright.resolve_element_locator", lambda page, ref: (None, "missing"))

    result = backend.click("task", "@e1")
    assert result["success"] is False
    assert "stale" in result["error"].lower()


def test_snapshot_generates_refs_and_tracks_frame_paths():
    backend = PatchrightBackend()

    child = _FakeFrame(
        elements=[
            {
                "role": "button",
                "name": "Inside frame",
                "text": "Inside frame",
                "selector": "button#in-frame",
                "xpath": "/html/body/button[1]",
                "visible": True,
                "enabled": True,
                "bbox": {"x": 1, "y": 1, "width": 10, "height": 10},
            }
        ]
    )
    root = _FakeFrame(
        elements=[
            {
                "role": "link",
                "name": "Home",
                "text": "Home",
                "selector": "a.home",
                "xpath": "/html/body/a[1]",
                "visible": True,
                "enabled": True,
                "bbox": {"x": 0, "y": 0, "width": 10, "height": 10},
            }
        ],
        child_frames=[child],
    )

    page = _FakePage(root)
    session = _session("task", page)
    backend._sessions.set("task", session)

    result = backend.snapshot("task", full=False)

    assert result["success"] is True
    assert result["element_count"] == 2
    assert "@e1" in session.ref_map
    assert "@e2" in session.ref_map
    assert session.ref_map["@e2"].frame_path == [0]


def test_snapshot_full_includes_iframe_text_content():
    backend = PatchrightBackend()

    child = _FakeFrame(page_text="Child frame text")
    root = _FakeFrame(child_frames=[child], page_text="Root frame text")

    page = _FakePage(root)
    session = _session("task-full", page)
    backend._sessions.set("task-full", session)

    result = backend.snapshot("task-full", full=True)

    assert result["success"] is True
    snapshot_text = result.get("snapshot", "")
    assert "Root frame text" in snapshot_text
    assert "Child frame text" in snapshot_text


def test_click_uses_resolved_locator_with_frame_path(monkeypatch):
    backend = PatchrightBackend()
    page = _FakePage(_FakeFrame())
    session = _session("task", page)
    session.ref_map = {
        "@e2": ElementRef(
            ref="@e2",
            selector="button#pay",
            frame_path=[0],
        )
    }
    backend._sessions.set("task", session)

    locator = _FakeLocator()

    def _resolver(page_obj, element_ref):
        assert element_ref.frame_path == [0]
        return locator, None

    monkeypatch.setattr("tools.browser_backends.patchright.resolve_element_locator", _resolver)

    result = backend.click("task", "@e2")
    assert result["success"] is True
    assert locator.clicked is True


class _FakeInitPage:
    def __init__(self):
        self.url = "about:blank"

    def on(self, event, callback):
        return None


class _FakeCDPContext:
    def __init__(self):
        self.pages = [_FakeInitPage()]

    def new_page(self):
        page = _FakeInitPage()
        self.pages.append(page)
        return page


class _FakeBrowserFromCDP:
    def __init__(self):
        self.contexts = [_FakeCDPContext()]


class _FakeChromium:
    def __init__(self):
        self.connected_to = None
        self.connect_kwargs = None
        self.launch_user_data_dir = None
        self.launch_kwargs = None

    def connect_over_cdp(self, url, **kwargs):
        self.connected_to = url
        self.connect_kwargs = kwargs
        return _FakeBrowserFromCDP()

    def launch_persistent_context(self, user_data_dir, **kwargs):
        self.launch_user_data_dir = user_data_dir
        self.launch_kwargs = kwargs

        class _Ctx:
            pages = [_FakeInitPage()]

        return _Ctx()


class _FakePlaywrightRuntime:
    def __init__(self):
        self.chromium = _FakeChromium()

    def stop(self):
        return None


class _FakeSyncPlaywrightFactory:
    def __init__(self, runtime):
        self._runtime = runtime

    def __call__(self):
        return self

    def start(self):
        return self._runtime


class _DisplayCheckingSyncPlaywrightFactory(_FakeSyncPlaywrightFactory):
    def __init__(self, runtime, expected_display):
        super().__init__(runtime)
        self.expected_display = expected_display

    def start(self):
        import os

        assert os.environ.get("DISPLAY") == self.expected_display
        return self._runtime


def test_init_session_patchright_uses_cdp_when_configured(monkeypatch):
    backend = PatchrightBackend()
    runtime = _FakePlaywrightRuntime()

    monkeypatch.setattr("tools.browser_backends.patchright._SYNC_PLAYWRIGHT", _FakeSyncPlaywrightFactory(runtime))
    monkeypatch.setattr("tools.browser_backends.patchright._patchright_config", lambda *_args, **_kwargs: {"cdp_url": "ws://127.0.0.1:9222"})
    monkeypatch.setattr("tools.browser_backends.patchright._resolve_patchright_cdp_url", lambda cfg: "ws://resolved-cdp")

    state = backend.init_session("task-cdp")

    assert isinstance(state, PatchrightRuntimeSession)
    assert state.metadata.get("mode") == "cdp"
    assert state.metadata.get("cdp_url") == "ws://resolved-cdp"
    assert state.metadata.get("cdp_created_context") is False
    assert state.metadata.get("cdp_created_page") is True
    assert runtime.chromium.connected_to == "ws://resolved-cdp"
    assert runtime.chromium.launch_user_data_dir is None


def test_patchright_is_local_false_when_cdp_url_configured(monkeypatch):
    backend = PatchrightBackend()
    monkeypatch.setattr("tools.browser_backends.patchright._patchright_config", lambda *_args, **_kwargs: {"cdp_url": "ws://remote-cdp"})
    monkeypatch.setattr("tools.browser_backends.patchright._resolve_patchright_cdp_url", lambda cfg: "ws://remote-cdp")

    assert backend.is_local() is False


def test_patchright_is_local_true_when_cdp_url_not_configured(monkeypatch):
    backend = PatchrightBackend()
    monkeypatch.setattr("tools.browser_backends.patchright._patchright_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("tools.browser_backends.patchright._resolve_patchright_cdp_url", lambda cfg: "")

    assert backend.is_local() is True


def test_patchright_supports_runtime_proxy_false_in_cdp_mode(monkeypatch):
    backend = PatchrightBackend()
    monkeypatch.setattr("tools.browser_backends.patchright._patchright_config", lambda *_args, **_kwargs: {"cdp_url": "ws://remote-cdp"})
    monkeypatch.setattr("tools.browser_backends.patchright._resolve_patchright_cdp_url", lambda cfg: "ws://remote-cdp")

    assert backend.supports_runtime_proxy("task-cdp") is False


def test_patchright_set_runtime_proxy_raises_in_cdp_mode(monkeypatch):
    backend = PatchrightBackend()
    monkeypatch.setattr("tools.browser_backends.patchright._patchright_config", lambda *_args, **_kwargs: {"cdp_url": "ws://remote-cdp"})
    monkeypatch.setattr("tools.browser_backends.patchright._resolve_patchright_cdp_url", lambda cfg: "ws://remote-cdp")

    with pytest.raises(RuntimeError):
        backend.set_runtime_proxy("task-cdp", {"server": "http://proxy.example:8080"})


def test_init_session_patchright_launches_local_chromium_without_cdp(monkeypatch):
    backend = PatchrightBackend()
    runtime = _FakePlaywrightRuntime()

    monkeypatch.setattr("tools.browser_backends.patchright._SYNC_PLAYWRIGHT", _FakeSyncPlaywrightFactory(runtime))
    monkeypatch.setattr("tools.browser_backends.patchright._patchright_config", lambda *_args, **_kwargs: {"headless": True})
    monkeypatch.setattr("tools.browser_backends.patchright._resolve_patchright_cdp_url", lambda cfg: "")
    monkeypatch.setattr("tools.browser_backends.patchright.tempfile.mkdtemp", lambda **kwargs: "/tmp/hermes-test-profile")

    state = backend.init_session("task-local")

    assert isinstance(state, PatchrightRuntimeSession)
    assert state.metadata.get("mode") == "launch"
    assert state.metadata.get("cdp_url") == ""
    assert runtime.chromium.connected_to is None
    assert runtime.chromium.launch_user_data_dir == "/tmp/hermes-test-profile"


def test_init_session_patchright_uses_executable_path_and_xvfb(monkeypatch):
    backend = PatchrightBackend()
    runtime = _FakePlaywrightRuntime()

    monkeypatch.setattr(
        "tools.browser_backends.patchright._SYNC_PLAYWRIGHT",
        _DisplayCheckingSyncPlaywrightFactory(runtime, expected_display=":99"),
    )
    monkeypatch.setattr(
        "tools.browser_backends.patchright._patchright_config",
        lambda *_args, **_kwargs: {
            "headless": False,
            "executable_path": "/usr/bin/google-chrome",
            "xvfb": {"enabled": True, "display": ":99", "screen": "1920x1080x24", "force": True},
        },
    )
    monkeypatch.setattr("tools.browser_backends.patchright._resolve_patchright_cdp_url", lambda cfg: "")
    monkeypatch.setattr("tools.browser_backends.patchright.tempfile.mkdtemp", lambda **kwargs: "/tmp/hermes-test-profile")
    monkeypatch.setattr("tools.browser_backends.patchright.shutil.which", lambda cmd: "/usr/bin/Xvfb")
    monkeypatch.setattr("tools.browser_backends.patchright._XVFB_PROCESS", None)
    monkeypatch.setattr("tools.browser_backends.patchright._XVFB_DISPLAY", None)
    monkeypatch.delenv("DISPLAY", raising=False)

    class _FakeProc:
        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("tools.browser_backends.patchright.subprocess.Popen", lambda *args, **kwargs: _FakeProc())

    state = backend.init_session("task-exec")

    assert isinstance(state, PatchrightRuntimeSession)
    assert runtime.chromium.launch_kwargs.get("executable_path") == "/usr/bin/google-chrome"


def test_init_session_patchright_includes_proxy_from_url(monkeypatch):
    backend = PatchrightBackend()
    runtime = _FakePlaywrightRuntime()

    monkeypatch.setattr("tools.browser_backends.patchright._SYNC_PLAYWRIGHT", _FakeSyncPlaywrightFactory(runtime))
    monkeypatch.setattr(
        "tools.browser_backends.patchright._patchright_config",
        lambda *_args, **_kwargs: {
            "headless": True,
            "proxy": {
                "url": "http://user123:pass456@proxy.example:8080",
            },
        },
    )
    monkeypatch.setattr("tools.browser_backends.patchright._resolve_patchright_cdp_url", lambda cfg: "")
    monkeypatch.setattr("tools.browser_backends.patchright.tempfile.mkdtemp", lambda **kwargs: "/tmp/hermes-test-profile")

    backend.init_session("task-proxy")

    assert runtime.chromium.launch_kwargs.get("proxy") == {
        "server": "http://proxy.example:8080",
        "username": "user123",
        "password": "pass456",
    }


def test_resolve_patchright_proxy_supports_string_and_overrides():
    from_string = patchright_mod._resolve_patchright_proxy(
        {"proxy": "sample-user:sample-pass@proxy.example:8080"}
    )
    assert from_string == {
        "server": "http://proxy.example:8080",
        "username": "sample-user",
        "password": "sample-pass",
    }

    from_dict = patchright_mod._resolve_patchright_proxy(
        {
            "proxy": {
                "url": "http://u1:p1@proxy.example:8080",
                "username": "u2",
                "password": "p2",
                "bypass": "localhost,127.0.0.1",
            }
        }
    )
    assert from_dict == {
        "server": "http://proxy.example:8080",
        "username": "u2",
        "password": "p2",
        "bypass": "localhost,127.0.0.1",
    }


def test_patchright_config_prefers_runtime_proxy_override(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"browser": {"patchright": {"headless": True, "proxy": {"server": "http://from-config:9000"}}}},
    )

    patchright_mod._set_patchright_proxy_override("task-runtime", {"server": "http://runtime:8080"})
    try:
        cfg = patchright_mod._patchright_config("task-runtime")
        assert cfg.get("proxy") == {"server": "http://runtime:8080"}
    finally:
        patchright_mod._set_patchright_proxy_override("task-runtime", None)


def test_close_session_clears_runtime_proxy_override_even_without_state():
    backend = PatchrightBackend()
    patchright_mod._set_patchright_proxy_override("task-runtime", {"server": "http://runtime:8080"})
    try:
        assert backend.close_session("task-runtime") is False
        assert patchright_mod._get_patchright_proxy_override("task-runtime") is None
    finally:
        patchright_mod._set_patchright_proxy_override("task-runtime", None)


class _CloseTracker:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _PlaywrightStopTracker:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_safe_close_patchright_state_closes_owned_cdp_resources():
    page = _CloseTracker()
    context = _CloseTracker()
    playwright = _PlaywrightStopTracker()
    now = time.time()
    state = PatchrightRuntimeSession(
        task_id="cdp-owned",
        started_at=now,
        last_activity=now,
        page=page,
        browser_context=context,
        playwright=playwright,
        metadata={"cdp_url": "ws://remote", "cdp_created_context": True, "cdp_created_page": True},
    )

    patchright_mod._safe_close_patchright_state(state)

    assert page.closed is True
    assert context.closed is True
    assert playwright.stopped is True


def test_safe_close_patchright_state_skips_shared_cdp_resources():
    page = _CloseTracker()
    context = _CloseTracker()
    playwright = _PlaywrightStopTracker()
    now = time.time()
    state = PatchrightRuntimeSession(
        task_id="cdp-shared",
        started_at=now,
        last_activity=now,
        page=page,
        browser_context=context,
        playwright=playwright,
        metadata={"cdp_url": "ws://remote", "cdp_created_context": False, "cdp_created_page": False},
    )

    patchright_mod._safe_close_patchright_state(state)

    assert page.closed is False
    assert context.closed is False
    assert playwright.stopped is True




PATCHRIGHT_AVAILABLE = importlib.util.find_spec("patchright") is not None


@pytest.mark.skipif(not PATCHRIGHT_AVAILABLE, reason="patchright is not installed in this environment")
class TestPatchrightBackendSmoke:
    def test_backend_reports_configured(self):
        backend = PatchrightBackend()
        assert backend.is_configured() is True
