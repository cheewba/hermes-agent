from __future__ import annotations

from typing import Any

from tools.browser_backend_base import ElementRef


INTERACTIVE_SELECTOR = ",".join(
    [
        "a[href]",
        "button",
        "input",
        "textarea",
        "select",
        "[role='button']",
        "[role='link']",
        "[role='checkbox']",
        "[role='radio']",
        "[contenteditable='true']",
    ]
)


ELEMENT_EXTRACTION_SCRIPT = r"""
(payload) => {
  const selector = payload.selector;
  const includeText = !!payload.includeText;

  const roleByTag = (el) => {
    const explicit = (el.getAttribute('role') || '').trim();
    if (explicit) return explicit.toLowerCase();
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'button' || t === 'submit' || t === 'reset') return 'button';
      return 'textbox';
    }
    if (el.getAttribute('contenteditable') === 'true') return 'textbox';
    return tag || 'element';
  };

  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const text = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();

  const byId = (el) => {
    if (!el.id) return null;
    const escaped = window.CSS && CSS.escape ? CSS.escape(el.id) : el.id;
    const candidate = `#${escaped}`;
    try {
      if (document.querySelectorAll(candidate).length === 1) return candidate;
    } catch (_) {}
    return null;
  };

  const byAttr = (el, name) => {
    const val = el.getAttribute(name);
    if (!val) return null;
    const candidate = `${el.tagName.toLowerCase()}[${name}="${String(val).replace(/"/g, '\\"')}"]`;
    try {
      if (document.querySelectorAll(candidate).length === 1) return candidate;
    } catch (_) {}
    return null;
  };

  const cssPath = (el) => {
    if (!(el instanceof Element)) return null;
    const idCandidate = byId(el);
    if (idCandidate) return idCandidate;
    for (const attr of ['data-testid', 'data-test', 'name', 'aria-label']) {
      const attrCandidate = byAttr(el, attr);
      if (attrCandidate) return attrCandidate;
    }

    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE && node !== document.body) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (!parent) {
        parts.unshift(part);
        break;
      }
      const siblings = Array.from(parent.children).filter(s => s.tagName === node.tagName);
      if (siblings.length > 1) {
        const idx = siblings.indexOf(node) + 1;
        part += `:nth-of-type(${idx})`;
      }
      parts.unshift(part);
      node = parent;
    }
    if (!parts.length) return null;
    return parts.join(' > ');
  };

  const xpath = (el) => {
    if (!(el instanceof Element)) return null;
    if (el.id) {
      return `//*[@id="${String(el.id).replace(/"/g, '\\"')}"]`;
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE) {
      const tag = node.tagName.toLowerCase();
      let index = 1;
      let sib = node.previousElementSibling;
      while (sib) {
        if (sib.tagName === node.tagName) index += 1;
        sib = sib.previousElementSibling;
      }
      parts.unshift(`${tag}[${index}]`);
      node = node.parentElement;
    }
    return '/' + parts.join('/');
  };

  const getName = (el) => {
    const ariaLabel = (el.getAttribute('aria-label') || '').trim();
    if (ariaLabel) return ariaLabel;
    const labelledBy = (el.getAttribute('aria-labelledby') || '').trim();
    if (labelledBy) {
      const chunks = labelledBy
        .split(/\s+/)
        .map(id => document.getElementById(id))
        .filter(Boolean)
        .map(node => text(node))
        .filter(Boolean);
      if (chunks.length) return chunks.join(' ');
    }

    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      const placeholder = (el.getAttribute('placeholder') || '').trim();
      if (placeholder) return placeholder;
      const name = (el.getAttribute('name') || '').trim();
      if (name) return name;
    }

    const alt = (el.getAttribute('alt') || '').trim();
    if (alt) return alt;
    const title = (el.getAttribute('title') || '').trim();
    if (title) return title;
    return text(el);
  };

  const attrNames = ['id', 'name', 'type', 'href', 'value', 'placeholder'];
  const out = [];
  const nodes = Array.from(document.querySelectorAll(selector));
  for (const el of nodes) {
    const role = roleByTag(el);
    const name = getName(el);
    const txt = text(el);
    const rect = el.getBoundingClientRect();
    const attrs = {};
    for (const attr of attrNames) {
      const val = el.getAttribute(attr);
      if (val) attrs[attr] = val;
    }
    out.push({
      role,
      name,
      text: txt,
      selector: cssPath(el),
      xpath: xpath(el),
      frame_path: [],
      bbox: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      visible: visible(el),
      enabled: !el.disabled,
      attributes: attrs,
    });
  }

  return {
    elements: out,
    page_text: includeText && document.body ? (document.body.innerText || '') : '',
  };
}
"""


def resolve_frame_by_path(page: Any, frame_path: list[int] | None) -> Any | None:
    frame = getattr(page, "main_frame", None)
    if frame is None:
        return None

    for idx in frame_path or []:
        children = list(getattr(frame, "child_frames", []) or [])
        if idx < 0 or idx >= len(children):
            return None
        frame = children[idx]
    return frame


def stale_ref_error() -> dict[str, Any]:
    return {
        "success": False,
        "error": "Element ref is stale; call browser_snapshot again",
    }


def _locator_count(locator: Any) -> int:
    try:
        return int(locator.count())
    except Exception:
        return 0


def _resolve_with_selector(frame: Any, selector: str) -> Any | None:
    if not selector:
        return None
    try:
        loc = frame.locator(selector)
    except Exception:
        return None
    if _locator_count(loc) > 0:
        return loc.first
    return None


def _resolve_with_xpath(frame: Any, xpath: str) -> Any | None:
    if not xpath:
        return None
    try:
        loc = frame.locator(f"xpath={xpath}")
    except Exception:
        return None
    if _locator_count(loc) > 0:
        return loc.first
    return None


def _resolve_with_role(frame: Any, element_ref: ElementRef) -> Any | None:
    role = (element_ref.role or "").strip().lower()
    name = (element_ref.name or "").strip()
    if not role:
        return None
    try:
        if name:
            loc = frame.get_by_role(role, name=name)
        else:
            loc = frame.get_by_role(role)
    except Exception:
        return None

    count = _locator_count(loc)
    if count == 1:
        return loc.first
    return None


def _resolve_with_text(frame: Any, element_ref: ElementRef) -> Any | None:
    text = (element_ref.text or "").strip()
    if not text:
        return None
    try:
        loc = frame.get_by_text(text)
    except Exception:
        return None
    count = _locator_count(loc)
    if count == 1:
        return loc.first
    return None


def resolve_element_locator(page: Any, element_ref: ElementRef) -> tuple[Any | None, str | None]:
    frame = resolve_frame_by_path(page, element_ref.frame_path)
    if frame is None:
        return None, "frame-not-found"

    locator = _resolve_with_selector(frame, element_ref.selector or "")
    if locator is None:
        locator = _resolve_with_xpath(frame, element_ref.xpath or "")
    if locator is None:
        locator = _resolve_with_role(frame, element_ref)
    if locator is None:
        locator = _resolve_with_text(frame, element_ref)
    if locator is None:
        return None, "element-not-found"
    return locator, None
