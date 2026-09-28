"""W4.3 — embedding model availability: retry in the background, single-flight.

The previous behaviour (PLAN-v2 §8.2 N6): `_model_failed` was cleared *before*
the retry attempt, so once the cooldown elapsed every concurrent request ran
`_load_model()` itself — N request threads, N parallel ~80 MB ONNX
download/load attempts, each blocking its own request. Now the first caller past
the cooldown schedules one daemon thread and everybody returns None immediately
(keyword search keeps working).
"""

import threading
import time

import app.embeddings as emb


def _reset(monkeypatch, failed: bool, failed_at: float | None = None):
    monkeypatch.setattr(emb, "_model", None)
    monkeypatch.setattr(emb, "_model_failed", failed)
    monkeypatch.setattr(emb, "_model_failed_at", failed_at)
    monkeypatch.setattr(emb, "_retry_thread", None)
    monkeypatch.setattr(emb, "_retry_scheduled", False)


def test_first_use_loads_synchronously_so_startup_can_validate(monkeypatch):
    """warm_up() must still block and see the result (STRICT semantics)."""
    _reset(monkeypatch, failed=False)
    sentinel = object()
    monkeypatch.setattr(emb, "_load_model", lambda: sentinel)
    assert emb._get_model() is sentinel
    assert emb.model_status() == "ready"


def test_failure_is_recorded_and_never_retried_inline(monkeypatch):
    _reset(monkeypatch, failed=False)
    monkeypatch.setattr(emb, "RETRY_SECONDS", 0.0)
    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        raise RuntimeError("transient failure")

    monkeypatch.setattr(emb, "_load_model", failing)
    assert emb._get_model() is None          # first attempt (startup path)
    assert calls["n"] == 1
    assert emb.model_status() == "failed"

    # Even with RETRY_SECONDS=0 the cooldown is floored at MIN_RETRY_SECONDS, so
    # a request cannot hammer a failing model.
    for _ in range(5):
        assert emb._get_model() is None
    assert calls["n"] == 1, "no request may retry inline"


def test_attempt_load_recovers_and_publishes(monkeypatch):
    _reset(monkeypatch, failed=True, failed_at=time.monotonic())
    monkeypatch.setattr(emb, "_load_model",
                        lambda: (_ for _ in ()).throw(RuntimeError("still down")))
    assert emb._attempt_load() is False
    assert emb.model_status() == "failed"

    sentinel = object()
    monkeypatch.setattr(emb, "_load_model", lambda: sentinel)
    assert emb._attempt_load() is True
    assert emb.model_status() == "ready"
    assert emb._get_model() is sentinel
    assert emb._model_failed is False and emb._model_failed_at is None


def test_background_retry_is_single_flight_under_concurrency(monkeypatch):
    """20 concurrent callers past the cooldown -> exactly ONE load attempt, and
    none of them blocks on it."""
    _reset(monkeypatch, failed=True,
           failed_at=time.monotonic() - emb.MIN_RETRY_SECONDS - 5)
    monkeypatch.setattr(emb, "RETRY_SECONDS", 0.0)
    calls = {"n": 0}
    lock = threading.Lock()

    def slow_failing():
        with lock:
            calls["n"] += 1
        time.sleep(0.3)                       # widen the overlap window
        raise RuntimeError("still down")

    monkeypatch.setattr(emb, "_load_model", slow_failing)

    results = []
    barrier = threading.Barrier(20)

    def caller():
        barrier.wait(timeout=30)
        started = time.monotonic()
        results.append((emb._get_model(), time.monotonic() - started))

    threads = [threading.Thread(target=caller) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert all(model is None for model, _ in results), "callers must not get a model"
    # Nobody waited for the retry: the attempt itself sleeps 0.3s.
    assert max(elapsed for _, elapsed in results) < 0.25, results
    # Give the single background thread time to finish its attempt.
    deadline = time.monotonic() + 5
    while emb.retry_in_flight() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert calls["n"] == 1, f"expected exactly one load attempt, got {calls['n']}"
    assert emb.model_status() == "failed"


def test_background_retry_publishes_a_model_that_later_requests_see(monkeypatch):
    _reset(monkeypatch, failed=True,
           failed_at=time.monotonic() - emb.MIN_RETRY_SECONDS - 5)
    monkeypatch.setattr(emb, "RETRY_SECONDS", 0.0)
    sentinel = object()
    monkeypatch.setattr(emb, "_load_model", lambda: (time.sleep(0.05), sentinel)[1])

    assert emb._get_model() is None            # scheduled, did not block
    deadline = time.monotonic() + 5
    while emb.retry_in_flight() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert emb._get_model() is sentinel
    assert emb.model_status() == "ready"


def test_schedule_retry_is_idempotent_while_running(monkeypatch):
    _reset(monkeypatch, failed=True, failed_at=0.0)
    started = threading.Event()
    release = threading.Event()

    def blocking_load():
        started.set()
        release.wait(timeout=10)
        raise RuntimeError("nope")

    monkeypatch.setattr(emb, "_load_model", blocking_load)
    assert emb._schedule_retry() is True
    assert started.wait(timeout=5)
    # While it runs, further scheduling is a no-op (the race N6 describes).
    assert emb._schedule_retry() is False
    assert emb._schedule_retry() is False
    release.set()
    deadline = time.monotonic() + 5
    while emb.retry_in_flight() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert emb.retry_in_flight() is False
    # …and a new one can be scheduled afterwards.
    monkeypatch.setattr(emb, "_model_failed_at", 0.0)
    release.set()
    assert emb._schedule_retry() is True
    deadline = time.monotonic() + 5
    while emb.retry_in_flight() and time.monotonic() < deadline:
        time.sleep(0.02)
