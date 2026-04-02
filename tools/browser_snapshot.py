from __future__ import annotations

import os
import re
from typing import Iterable

from agent.auxiliary_client import call_llm
from tools.browser_backend_base import ElementRef


_REF_NUM_RE = re.compile(r"^@?e(\d+)$", re.IGNORECASE)
SNAPSHOT_SUMMARIZE_THRESHOLD = 8000


def normalize_ref(ref: str) -> str:
    value = (ref or "").strip()
    if not value:
        return ""
    match = _REF_NUM_RE.match(value)
    if not match:
        return value if value.startswith("@") else f"@{value}"
    return f"@e{int(match.group(1))}"


def _role_for_display(el: ElementRef) -> str:
    role = (el.role or "").strip().lower()
    if role:
        return role
    return "element"


def _text_for_display(el: ElementRef) -> str:
    for candidate in (el.name, el.text):
        val = (candidate or "").strip()
        if val:
            return val
    return ""


def render_element_line(el: ElementRef) -> str:
    role = _role_for_display(el)
    ref = normalize_ref(el.ref)
    text = _text_for_display(el)

    frame_hint = ""
    if el.frame_path:
        frame_hint = f" frame={'/'.join(str(i) for i in el.frame_path)}"

    if text:
        return f"[{role} {ref}{frame_hint}] {text}"
    return f"[{role} {ref}{frame_hint}]"


def _ref_sort_key(ref: str) -> tuple[int, str]:
    match = _REF_NUM_RE.match(ref or "")
    if match:
        return (int(match.group(1)), ref)
    return (10**9, ref)


def render_snapshot(elements: Iterable[ElementRef], *, full: bool = False, page_text: str = "") -> str:
    lines: list[str] = []
    for el in sorted(elements, key=lambda item: _ref_sort_key(item.ref)):
        lines.append(render_element_line(el))

    if not full:
        return "\n".join(lines)

    body = (page_text or "").strip()
    if not body:
        return "\n".join(lines)

    if lines:
        return f"{body}\n\n--- Interactive elements ---\n" + "\n".join(lines)
    return body


def truncate_snapshot(snapshot_text: str, max_chars: int = 8000) -> str:
    if len(snapshot_text) <= max_chars:
        return snapshot_text
    return snapshot_text[:max_chars] + "\n\n[... content truncated ...]"


def _get_extraction_model() -> str | None:
    return os.getenv("AUXILIARY_WEB_EXTRACT_MODEL", "").strip() or None


def extract_relevant_content(snapshot_text: str, user_task: str | None = None) -> str:
    if user_task:
        extraction_prompt = (
            "You are a content extractor for a browser automation agent.\n\n"
            f"The user's task is: {user_task}\n\n"
            "Given the following page snapshot (accessibility tree representation), "
            "extract and summarize the most relevant information for completing this task. Focus on:\n"
            "1. Interactive elements (buttons, links, inputs) that might be needed\n"
            "2. Text content relevant to the task (prices, descriptions, headings, important info)\n"
            "3. Navigation structure if relevant\n\n"
            "Keep ref IDs (like [ref=e5]) for interactive elements so the agent can use them.\n\n"
            f"Page Snapshot:\n{snapshot_text}\n\n"
            "Provide a concise summary that preserves actionable information and relevant content."
        )
    else:
        extraction_prompt = (
            "Summarize this page snapshot, preserving:\n"
            "1. All interactive elements with their ref IDs (like [ref=e5])\n"
            "2. Key text content and headings\n"
            "3. Important information visible on the page\n\n"
            f"Page Snapshot:\n{snapshot_text}\n\n"
            "Provide a concise summary focused on interactive elements and key content."
        )

    from agent.redact import redact_sensitive_text

    extraction_prompt = redact_sensitive_text(extraction_prompt)

    try:
        call_kwargs = {
            "task": "web_extract",
            "messages": [{"role": "user", "content": extraction_prompt}],
            "max_tokens": 4000,
            "temperature": 0.1,
        }
        model = _get_extraction_model()
        if model:
            call_kwargs["model"] = model
        response = call_llm(**call_kwargs)
        extracted = (response.choices[0].message.content or "").strip() or truncate_snapshot(snapshot_text)
        return redact_sensitive_text(extracted)
    except Exception:
        return truncate_snapshot(snapshot_text)
