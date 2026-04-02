from unittest.mock import Mock, patch


HOST = "example-host"
PORT = 9223
WS_URL = f"ws://{HOST}:{PORT}/devtools/browser/abc123"
HTTP_URL = f"http://{HOST}:{PORT}"
VERSION_URL = f"{HTTP_URL}/json/version"


class TestResolveCdpOverride:
    def test_keeps_full_devtools_websocket_url(self):
        from tools.browser_tool import _resolve_cdp_override

        assert _resolve_cdp_override(WS_URL) == WS_URL

    def test_resolves_http_discovery_endpoint_to_websocket(self):
        from tools.browser_tool import _resolve_cdp_override

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("tools.browser_backends.agent_browser.requests.get", return_value=response) as mock_get:
            resolved = _resolve_cdp_override(HTTP_URL)

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)

    def test_resolves_bare_ws_hostport_to_discovery_websocket(self):
        from tools.browser_tool import _resolve_cdp_override

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("tools.browser_backends.agent_browser.requests.get", return_value=response) as mock_get:
            resolved = _resolve_cdp_override(f"ws://{HOST}:{PORT}")

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)

    def test_falls_back_to_raw_url_when_discovery_fails(self):
        from tools.browser_tool import _resolve_cdp_override

        with patch("tools.browser_backends.agent_browser.requests.get", side_effect=RuntimeError("boom")):
            assert _resolve_cdp_override(HTTP_URL) == HTTP_URL


class TestAgentBrowserLocality:
    def test_is_local_false_when_cdp_override_set(self, monkeypatch):
        from tools.browser_backends.agent_browser import AgentBrowserBackend

        backend = AgentBrowserBackend()
        monkeypatch.setattr(backend, "_get_cloud_provider", lambda: None)
        monkeypatch.setenv("BROWSER_CDP_URL", "ws://remote-host:9222/devtools/browser/abc")

        assert backend.is_local() is False

    def test_is_local_true_without_cdp_and_cloud(self, monkeypatch):
        from tools.browser_backends.agent_browser import AgentBrowserBackend

        backend = AgentBrowserBackend()
        monkeypatch.setattr(backend, "_get_cloud_provider", lambda: None)
        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

        assert backend.is_local() is True
