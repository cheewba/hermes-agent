"""Tests for Homebrew PATH discovery in AgentBrowserBackend."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.browser_backends import agent_browser


class TestSanePath:
    def test_includes_homebrew_bin(self):
        assert "/opt/homebrew/bin" in agent_browser._SANE_PATH

    def test_includes_homebrew_sbin(self):
        assert "/opt/homebrew/sbin" in agent_browser._SANE_PATH

    def test_includes_standard_dirs(self):
        assert "/usr/local/bin" in agent_browser._SANE_PATH
        assert "/usr/bin" in agent_browser._SANE_PATH
        assert "/bin" in agent_browser._SANE_PATH


class TestDiscoverHomebrewNodeDirs:
    def test_returns_empty_when_no_homebrew(self):
        with patch("os.path.isdir", return_value=False):
            assert agent_browser._discover_homebrew_node_dirs() == []

    def test_finds_versioned_node_dirs(self):
        entries = ["node@20", "node@24", "openssl", "node", "python@3.12"]

        def mock_isdir(p):
            if p == "/opt/homebrew/opt":
                return True
            return p in (
                "/opt/homebrew/opt/node@20/bin",
                "/opt/homebrew/opt/node@24/bin",
            )

        with patch("os.path.isdir", side_effect=mock_isdir), patch("os.listdir", return_value=entries):
            result = agent_browser._discover_homebrew_node_dirs()

        assert "/opt/homebrew/opt/node@20/bin" in result
        assert "/opt/homebrew/opt/node@24/bin" in result

    def test_handles_oserror_gracefully(self):
        with patch("os.path.isdir", return_value=True), patch("os.listdir", side_effect=OSError("denied")):
            assert agent_browser._discover_homebrew_node_dirs() == []


class TestFindAgentBrowser:
    def test_finds_in_current_path(self):
        with patch("shutil.which", return_value="/usr/local/bin/agent-browser"):
            assert agent_browser.find_agent_browser() == "/usr/local/bin/agent-browser"

    def test_finds_npx_in_homebrew(self):
        def mock_which(cmd, path=None):
            if cmd == "agent-browser":
                return None
            if cmd == "npx" and path and "/opt/homebrew/bin" in path:
                return "/opt/homebrew/bin/npx"
            return None

        original_exists = Path.exists

        def mock_exists(self):
            if "node_modules" in str(self) and "agent-browser" in str(self):
                return False
            return original_exists(self)

        with (
            patch("shutil.which", side_effect=mock_which),
            patch("os.path.isdir", return_value=True),
            patch.object(Path, "exists", mock_exists),
            patch("tools.browser_backends.agent_browser._discover_homebrew_node_dirs", return_value=[]),
        ):
            assert agent_browser.find_agent_browser() == "npx agent-browser"

    def test_raises_when_not_found(self):
        original_exists = Path.exists

        def mock_exists(self):
            if "node_modules" in str(self) and "agent-browser" in str(self):
                return False
            return original_exists(self)

        with (
            patch("shutil.which", return_value=None),
            patch("os.path.isdir", return_value=False),
            patch.object(Path, "exists", mock_exists),
            patch("tools.browser_backends.agent_browser._discover_homebrew_node_dirs", return_value=[]),
        ):
            with pytest.raises(FileNotFoundError, match="agent-browser CLI not found"):
                agent_browser.find_agent_browser()


class TestBuildBrowserPath:
    def test_includes_homebrew_node_dirs(self):
        fake_homebrew_dirs = ["/opt/homebrew/opt/node@24/bin", "/opt/homebrew/opt/node@20/bin"]
        real_isdir = os.path.isdir

        def selective_isdir(p):
            if p in fake_homebrew_dirs:
                return True
            if "/opt/homebrew/" in p:
                return True
            if p.endswith("/.hermes/node/bin"):
                return True
            return real_isdir(p)

        with (
            patch("tools.browser_backends.agent_browser._discover_homebrew_node_dirs", return_value=fake_homebrew_dirs),
            patch("os.path.isdir", side_effect=selective_isdir),
            patch("tools.browser_backends.agent_browser.get_hermes_home", return_value=Path("/home/test/.hermes")),
        ):
            result_path = agent_browser._build_browser_path("/usr/bin:/bin")

        assert "/opt/homebrew/opt/node@24/bin" in result_path
        assert "/opt/homebrew/opt/node@20/bin" in result_path
        assert "/opt/homebrew/bin" in result_path
