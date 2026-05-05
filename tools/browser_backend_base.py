from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ElementRef:
    """Normalized element reference stored between snapshot and interactions."""

    ref: str
    role: str | None = None
    name: str | None = None
    text: str | None = None
    selector: str | None = None
    xpath: str | None = None
    frame_path: list[int] | None = None
    bbox: dict[str, Any] | None = None
    visible: bool = True
    enabled: bool = True
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class BrowserSessionState:
    """Shared browser session metadata for all backends."""

    task_id: str
    started_at: float
    last_activity: float
    current_url: str | None = None
    ref_version: int = 0
    ref_map: dict[str, ElementRef] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class BrowserBackend(ABC):
    """Common browser backend interface used by tools/browser_tool.py."""

    @abstractmethod
    def backend_name(self) -> str:
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        pass

    @abstractmethod
    def is_local(self) -> bool:
        """Whether SSRF private-IP checks should be skipped for this backend."""

    @abstractmethod
    def init_session(self, task_id: str) -> BrowserSessionState:
        pass

    @abstractmethod
    def get_session(self, task_id: str) -> BrowserSessionState | None:
        pass

    @abstractmethod
    def list_sessions(self) -> list[BrowserSessionState]:
        pass

    @abstractmethod
    def close_session(self, task_id: str) -> bool:
        pass

    @abstractmethod
    def emergency_cleanup(self, task_id: str) -> None:
        pass

    @abstractmethod
    def navigate(self, task_id: str, url: str) -> dict[str, Any]:
        pass

    @abstractmethod
    def snapshot(self, task_id: str, full: bool = False) -> dict[str, Any]:
        pass

    @abstractmethod
    def click(self, task_id: str, ref: str) -> dict[str, Any]:
        pass

    @abstractmethod
    def type(self, task_id: str, ref: str, text: str) -> dict[str, Any]:
        pass

    @abstractmethod
    def scroll(self, task_id: str, direction: str) -> dict[str, Any]:
        pass

    @abstractmethod
    def press(self, task_id: str, key: str) -> dict[str, Any]:
        pass

    @abstractmethod
    def back(self, task_id: str) -> dict[str, Any]:
        pass

    @abstractmethod
    def get_images(self, task_id: str) -> dict[str, Any]:
        pass

    @abstractmethod
    def vision(self, task_id: str, question: str, annotate: bool = False) -> dict[str, Any]:
        pass

    @abstractmethod
    def console(self, task_id: str, clear: bool = False) -> dict[str, Any]:
        pass

    @abstractmethod
    def solve_cloudflare(self, task_id: str, max_wait_seconds: int = 120) -> dict[str, Any]:
        pass
