from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

from tools.browser_backend_base import BrowserSessionState

TState = TypeVar("TState", bound=BrowserSessionState)


class BrowserSessionStore(Generic[TState]):
    """Thread-safe session store used by browser backends.

    Stores state by task_id and provides lifecycle helpers used by
    backend-level inactivity cleanup and orchestration.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, TState] = {}
        self._inflight: dict[str, threading.Event] = {}

    def get(self, task_id: str) -> TState | None:
        with self._lock:
            return self._sessions.get(task_id)

    def get_or_create(self, task_id: str, factory: Callable[[], TState]) -> TState:
        waiter: threading.Event | None = None

        while True:
            with self._lock:
                existing = self._sessions.get(task_id)
                if existing is not None:
                    return existing

                inflight = self._inflight.get(task_id)
                if inflight is None:
                    inflight = threading.Event()
                    self._inflight[task_id] = inflight
                    waiter = None
                    break

                waiter = inflight

            waiter.wait()

        try:
            created = factory()
        except Exception:
            with self._lock:
                inflight = self._inflight.pop(task_id, None)
                if inflight is not None:
                    inflight.set()
            raise

        with self._lock:
            existing = self._sessions.get(task_id)
            if existing is not None:
                inflight = self._inflight.pop(task_id, None)
                if inflight is not None:
                    inflight.set()
                return existing

            self._sessions[task_id] = created
            inflight = self._inflight.pop(task_id, None)
            if inflight is not None:
                inflight.set()
            return created

    def set(self, task_id: str, state: TState) -> TState:
        with self._lock:
            self._sessions[task_id] = state
            return state

    def remove(self, task_id: str) -> TState | None:
        with self._lock:
            return self._sessions.pop(task_id, None)

    def touch(self, task_id: str, at: float | None = None) -> TState | None:
        with self._lock:
            state = self._sessions.get(task_id)
            if state is None:
                return None
            state.last_activity = time.time() if at is None else at
            return state

    def items(self) -> list[tuple[str, TState]]:
        with self._lock:
            return list(self._sessions.items())

    def values(self) -> list[TState]:
        with self._lock:
            return list(self._sessions.values())

    def task_ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
