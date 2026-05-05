"""Tests for browser_console and browser_vision orchestration in browser_tool."""

import json
import os

import pytest


class _FakeBackend:
    def __init__(self):
        self.console_calls = []
        self.vision_calls = []

    def console(self, task_id: str, clear: bool = False):
        self.console_calls.append((task_id, clear))
        return {
            "success": True,
            "console_messages": [{"type": "log", "text": "hello", "source": "console"}],
            "js_errors": [{"message": "Uncaught TypeError", "source": "exception"}],
            "total_messages": 1,
            "total_errors": 1,
        }

    def vision(self, task_id: str, question: str, annotate: bool = False):
        self.vision_calls.append((task_id, question, annotate))
        return {
            "success": True,
            "analysis": "Looks good",
            "screenshot_path": "/tmp/fake.png",
        }


class TestBrowserConsole:
    def test_returns_console_messages_and_errors(self, monkeypatch):
        from tools.browser_tool import browser_console

        backend = _FakeBackend()
        monkeypatch.setattr("tools.browser_tool._get_backend", lambda: backend)

        result = json.loads(browser_console(task_id="test"))

        assert result["success"] is True
        assert result["total_messages"] == 1
        assert result["total_errors"] == 1
        assert result["console_messages"][0]["text"] == "hello"
        assert result["js_errors"][0]["message"] == "Uncaught TypeError"

    def test_passes_clear_flag(self, monkeypatch):
        from tools.browser_tool import browser_console

        backend = _FakeBackend()
        monkeypatch.setattr("tools.browser_tool._get_backend", lambda: backend)

        _ = browser_console(clear=True, task_id="test")
        assert backend.console_calls == [("test", True)]


class TestBrowserConsoleSchema:
    def test_schema_in_browser_schemas(self):
        from tools.browser_tool import BROWSER_TOOL_SCHEMAS

        names = [s["name"] for s in BROWSER_TOOL_SCHEMAS]
        assert "browser_console" in names

    def test_schema_has_clear_param(self):
        from tools.browser_tool import BROWSER_TOOL_SCHEMAS

        schema = next(s for s in BROWSER_TOOL_SCHEMAS if s["name"] == "browser_console")
        props = schema["parameters"]["properties"]
        assert "clear" in props
        assert props["clear"]["type"] == "boolean"


class TestBrowserConsoleToolsetWiring:
    def test_in_browser_toolset(self):
        from toolsets import TOOLSETS

        assert "browser_console" in TOOLSETS["browser"]["tools"]

    def test_in_hermes_core_tools(self):
        from toolsets import _HERMES_CORE_TOOLS

        assert "browser_console" in _HERMES_CORE_TOOLS

    def test_in_legacy_toolset_map(self):
        from model_tools import _LEGACY_TOOLSET_MAP

        assert "browser_console" in _LEGACY_TOOLSET_MAP["browser_tools"]

    def test_in_registry(self):
        from tools.registry import registry
        from tools import browser_tool  # noqa: F401

        assert "browser_console" in registry._tools


class TestBrowserSetProxyToolsetWiring:
    def test_in_browser_toolset(self):
        from toolsets import TOOLSETS

        assert "browser_set_proxy" in TOOLSETS["browser"]["tools"]

    def test_in_hermes_core_tools(self):
        from toolsets import _HERMES_CORE_TOOLS

        assert "browser_set_proxy" in _HERMES_CORE_TOOLS

    def test_in_legacy_toolset_map(self):
        from model_tools import _LEGACY_TOOLSET_MAP

        assert "browser_set_proxy" in _LEGACY_TOOLSET_MAP["browser_tools"]

    def test_in_registry(self):
        from tools.registry import registry
        from tools import browser_tool  # noqa: F401

        assert "browser_set_proxy" in registry._tools

    def test_check_fn_blocks_non_patchright_backends(self, monkeypatch):
        from tools import browser_tool

        class _NonPatchrightBackend:
            def is_configured(self):
                return True

        monkeypatch.setattr(browser_tool, "_get_backend", lambda: _NonPatchrightBackend())
        assert browser_tool.check_browser_set_proxy_requirements() is False


class TestBrowserSolveHCaptchaToolsetWiring:
    def test_in_browser_toolset(self):
        from toolsets import TOOLSETS

        assert "browser_solve_hcaptcha" in TOOLSETS["browser"]["tools"]

    def test_in_hermes_core_tools(self):
        from toolsets import _HERMES_CORE_TOOLS

        assert "browser_solve_hcaptcha" in _HERMES_CORE_TOOLS

    def test_in_legacy_toolset_map(self):
        from model_tools import _LEGACY_TOOLSET_MAP

        assert "browser_solve_hcaptcha" in _LEGACY_TOOLSET_MAP["browser_tools"]

    def test_in_registry(self):
        from tools.registry import registry
        from tools import browser_tool  # noqa: F401

        assert "browser_solve_hcaptcha" in registry._tools


class TestBrowserVisionAnnotate:
    def test_schema_has_annotate_param(self):
        from tools.browser_tool import BROWSER_TOOL_SCHEMAS

        schema = next(s for s in BROWSER_TOOL_SCHEMAS if s["name"] == "browser_vision")
        props = schema["parameters"]["properties"]
        assert "annotate" in props
        assert props["annotate"]["type"] == "boolean"

    def test_annotate_flag_propagated(self, monkeypatch):
        from tools.browser_tool import browser_vision

        backend = _FakeBackend()
        monkeypatch.setattr("tools.browser_tool._get_backend", lambda: backend)

        result = json.loads(browser_vision("what is this", annotate=True, task_id="tid"))

        assert result["success"] is True
        assert backend.vision_calls == [("tid", "what is this", True)]


class TestRecordSessionsConfig:
    def test_default_config_has_record_sessions(self):
        from hermes_cli.config import DEFAULT_CONFIG

        browser_cfg = DEFAULT_CONFIG.get("browser", {})
        assert "record_sessions" in browser_cfg
        assert browser_cfg["record_sessions"] is False


class TestDogfoodSkill:
    """Dogfood skill files exist and have correct structure."""

    @pytest.fixture(autouse=True)
    def _skill_dir(self):
        self.skill_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "skills", "dogfood"
        )

    def test_skill_md_exists(self):
        assert os.path.exists(os.path.join(self.skill_dir, "SKILL.md"))

    def test_taxonomy_exists(self):
        assert os.path.exists(
            os.path.join(self.skill_dir, "references", "issue-taxonomy.md")
        )

    def test_report_template_exists(self):
        assert os.path.exists(
            os.path.join(self.skill_dir, "templates", "dogfood-report-template.md")
        )

    def test_skill_md_has_frontmatter(self):
        with open(os.path.join(self.skill_dir, "SKILL.md")) as f:
            content = f.read()
        assert content.startswith("---")
        assert "name: dogfood" in content
        assert "description:" in content

    def test_skill_references_browser_console(self):
        with open(os.path.join(self.skill_dir, "SKILL.md")) as f:
            content = f.read()
        assert "browser_console" in content

    def test_skill_references_annotate(self):
        with open(os.path.join(self.skill_dir, "SKILL.md")) as f:
            content = f.read()
        assert "annotate" in content

    def test_taxonomy_has_severity_levels(self):
        with open(
            os.path.join(self.skill_dir, "references", "issue-taxonomy.md")
        ) as f:
            content = f.read()
        assert "Critical" in content
        assert "High" in content
        assert "Medium" in content
        assert "Low" in content

    def test_taxonomy_has_categories(self):
        with open(
            os.path.join(self.skill_dir, "references", "issue-taxonomy.md")
        ) as f:
            content = f.read()
        assert "Functional" in content
        assert "Visual" in content
        assert "Accessibility" in content
        assert "Console" in content
