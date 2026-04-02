import threading
import time

from tools.browser_backend_base import BrowserSessionState
from tools.browser_session_store import BrowserSessionStore


def _state(task_id: str) -> BrowserSessionState:
    now = time.time()
    return BrowserSessionState(task_id=task_id, started_at=now, last_activity=now)


def test_get_or_create_returns_existing_instance():
    store: BrowserSessionStore[BrowserSessionState] = BrowserSessionStore()
    first = store.get_or_create("t1", lambda: _state("t1"))
    second = store.get_or_create("t1", lambda: _state("t1"))
    assert first is second


def test_touch_updates_last_activity():
    store: BrowserSessionStore[BrowserSessionState] = BrowserSessionStore()
    state = store.get_or_create("t1", lambda: _state("t1"))
    original = state.last_activity
    store.touch("t1", at=original + 10)
    assert store.get("t1").last_activity == original + 10


def test_remove_deletes_session():
    store: BrowserSessionStore[BrowserSessionState] = BrowserSessionStore()
    store.get_or_create("t1", lambda: _state("t1"))
    removed = store.remove("t1")
    assert removed is not None
    assert store.get("t1") is None


def test_items_and_task_ids_reflect_current_state():
    store: BrowserSessionStore[BrowserSessionState] = BrowserSessionStore()
    store.set("a", _state("a"))
    store.set("b", _state("b"))
    assert set(store.task_ids()) == {"a", "b"}
    assert {task for task, _ in store.items()} == {"a", "b"}


def test_get_or_create_serializes_factory_per_task_under_concurrency():
    store: BrowserSessionStore[BrowserSessionState] = BrowserSessionStore()
    barrier = threading.Barrier(2)
    factory_calls: list[str] = []
    results: list[BrowserSessionState] = []

    def _factory() -> BrowserSessionState:
        factory_calls.append("called")
        time.sleep(0.05)
        return _state("same-task")

    def _worker() -> None:
        barrier.wait()
        results.append(store.get_or_create("same-task", _factory))

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert len(factory_calls) == 1
    assert len(results) == 2
    assert results[0] is results[1]


def test_get_or_create_wakes_waiters_after_factory_failure_then_retries_once():
    store: BrowserSessionStore[BrowserSessionState] = BrowserSessionStore()
    barrier = threading.Barrier(2)
    fail_once = {"value": True}
    factory_calls: list[str] = []
    errors: list[Exception] = []
    results: list[BrowserSessionState] = []

    def _factory() -> BrowserSessionState:
        factory_calls.append("called")
        if fail_once["value"]:
            fail_once["value"] = False
            time.sleep(0.02)
            raise RuntimeError("boom")
        return _state("retry-task")

    def _worker() -> None:
        barrier.wait()
        try:
            results.append(store.get_or_create("retry-task", _factory))
        except Exception as exc:  # pragma: no cover - assertion below validates exact count
            errors.append(exc)

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert len(factory_calls) == 2
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert len(results) == 1
    assert store.get("retry-task") is results[0]
