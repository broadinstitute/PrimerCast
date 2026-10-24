"""Tests for gui.run_gate admission control (stdlib-only; no Streamlit)."""

import sys
import threading
import time
from pathlib import Path

import pytest

# gui/ lives at the repo root (not under src/), so make it importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import run_gate  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_gate():
    run_gate._reset_for_tests()
    yield
    run_gate._reset_for_tests()


def test_admits_up_to_the_limit(monkeypatch):
    monkeypatch.setattr(run_gate, "MAX_CONCURRENT_RUNS", 2)

    a, b, c = (run_gate.take_ticket() for _ in range(3))
    assert run_gate.try_start(a) == (True, 0)
    assert run_gate.try_start(b) == (True, 0)

    admitted, ahead = run_gate.try_start(c)
    assert not admitted
    assert ahead == 0  # head of the queue, but both slots are busy
    assert run_gate.stats() == (2, 1)


def test_release_hands_the_slot_to_the_next_in_line(monkeypatch):
    monkeypatch.setattr(run_gate, "MAX_CONCURRENT_RUNS", 1)

    a, b = run_gate.take_ticket(), run_gate.take_ticket()
    assert run_gate.try_start(a)[0]
    assert not run_gate.try_start(b)[0]

    run_gate.release(a)
    assert run_gate.try_start(b) == (True, 0)
    assert run_gate.stats() == (1, 0)


def test_queue_is_first_come_first_served(monkeypatch):
    monkeypatch.setattr(run_gate, "MAX_CONCURRENT_RUNS", 1)

    a, b, c = (run_gate.take_ticket() for _ in range(3))
    run_gate.try_start(a)

    # c must not jump ahead of b just because it polled first.
    assert run_gate.try_start(c) == (False, 1)
    assert run_gate.try_start(b) == (False, 0)

    run_gate.release(a)
    assert not run_gate.try_start(c)[0]
    assert run_gate.try_start(b)[0]


def test_release_is_idempotent(monkeypatch):
    """Both the run's finally and the Stop button can release one ticket."""
    monkeypatch.setattr(run_gate, "MAX_CONCURRENT_RUNS", 1)

    a = run_gate.take_ticket()
    assert run_gate.try_start(a)[0]
    run_gate.release(a)
    run_gate.release(a)
    assert run_gate.stats() == (0, 0)

    # The double release must not have created a phantom free slot.
    b, c = run_gate.take_ticket(), run_gate.take_ticket()
    assert run_gate.try_start(b)[0]
    assert not run_gate.try_start(c)[0]


def test_admitted_ticket_stays_admitted(monkeypatch):
    """A rerun that re-polls an already-running ticket must not re-queue it."""
    monkeypatch.setattr(run_gate, "MAX_CONCURRENT_RUNS", 1)

    a = run_gate.take_ticket()
    assert run_gate.try_start(a) == (True, 0)
    assert run_gate.try_start(a) == (True, 0)
    assert run_gate.stats() == (1, 0)


def test_waiter_that_stops_polling_is_dropped(monkeypatch):
    """A closed tab must not hold the head of the queue forever."""
    monkeypatch.setattr(run_gate, "MAX_CONCURRENT_RUNS", 1)
    monkeypatch.setattr(run_gate, "WAITER_TTL", 0.05)

    a, dead, b = (run_gate.take_ticket() for _ in range(3))
    run_gate.try_start(a)
    run_gate.try_start(dead)          # queues at the head, then goes silent

    time.sleep(0.1)
    run_gate.release(a)

    # b keeps polling, so it survives the prune and inherits the slot.
    assert run_gate.try_start(b) == (True, 0)
    assert run_gate.stats() == (1, 0)


def test_pruned_waiter_that_returns_rejoins_the_queue(monkeypatch):
    """Navigating away drops the ticket; coming back must not spin forever."""
    monkeypatch.setattr(run_gate, "MAX_CONCURRENT_RUNS", 1)
    monkeypatch.setattr(run_gate, "WAITER_TTL", 0.05)

    a = run_gate.take_ticket()
    run_gate.try_start(a)
    stale = run_gate.take_ticket()
    run_gate.try_start(stale)

    time.sleep(0.1)
    run_gate.try_start(a)          # a prune pass drops `stale`
    assert run_gate.stats() == (1, 0)

    run_gate.release(a)
    assert run_gate.try_start(stale) == (True, 0)


def test_concurrent_claims_never_exceed_the_limit(monkeypatch):
    """The gate is reached from one ScriptRunner thread per session."""
    monkeypatch.setattr(run_gate, "MAX_CONCURRENT_RUNS", 3)

    admitted = []
    lock = threading.Lock()
    start = threading.Barrier(20)

    def _session():
        ticket = run_gate.take_ticket()
        start.wait()
        if run_gate.try_start(ticket)[0]:
            with lock:
                admitted.append(ticket)

    threads = [threading.Thread(target=_session) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(admitted) == 3
    assert run_gate.stats() == (3, 17)
