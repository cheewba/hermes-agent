"""Tests for None-guard behavior in browser snapshot summarization helpers."""

import types
from unittest.mock import patch


def _make_response(content):
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message)
    return types.SimpleNamespace(choices=[choice])


class TestExtractRelevantContentNoneGuard:
    def test_none_content_falls_back_to_truncated(self):
        with patch("tools.browser_snapshot.call_llm", return_value=_make_response(None)):
            from tools.browser_snapshot import extract_relevant_content

            result = extract_relevant_content("This is a long snapshot text", "find the button")

        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0

    def test_normal_content_returned(self):
        with patch("tools.browser_snapshot.call_llm", return_value=_make_response("Extracted content here")):
            from tools.browser_snapshot import extract_relevant_content

            result = extract_relevant_content("snapshot text", "task")

        assert result == "Extracted content here"

    def test_empty_string_content_falls_back(self):
        with patch("tools.browser_snapshot.call_llm", return_value=_make_response("   ")):
            from tools.browser_snapshot import extract_relevant_content

            result = extract_relevant_content("This is a long snapshot text", "task")

        assert result is not None
        assert len(result) > 0


class TestVisionNoneFallbackExpression:
    def test_none_content_produces_fallback_message(self):
        response = _make_response(None)
        analysis = (response.choices[0].message.content or "").strip()
        fallback = analysis or "Vision analysis returned no content."
        assert fallback == "Vision analysis returned no content."

    def test_normal_content_passes_through(self):
        response = _make_response("  The page shows a login form.  ")
        analysis = (response.choices[0].message.content or "").strip()
        fallback = analysis or "Vision analysis returned no content."
        assert fallback == "The page shows a login form."
