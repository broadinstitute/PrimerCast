"""Admission control for pipeline runs sharing one Cloud Run instance.

Streamlit serves several browser sessions from one process, and each design /
evaluate run spawns a snakemake subprocess that asks for all of the instance's
cores (``gui.app.CORES``) and writes its scratch files to tmpfs -- which, on
Cloud Run, counts against the instance's memory. Nothing stopped every session
from starting a run at once: the CPU oversubscribes, and if the combined
scratch exceeds the memory limit the container is OOM-killed, which drops
*every* session on that instance (Streamlit's ``session_state`` lives in the
process), not just the one that asked for too much.

This module caps the number of runs executing concurrently in one process. The
limit is deliberately per-process rather than global: the resource being
protected -- cores and tmpfs -- is per-instance, and Cloud Run adds instances
independently.

Waiters are served first-come-first-served. ``try_start`` doubles as a
heartbeat, so a session that stops polling (closed tab, dropped websocket) is
dropped from the queue instead of holding the head of the line forever.

Standard library only, so it can be unit tested without importing Streamlit.
"""

from __future__ import annotations

import itertools
import os
import threading
import time

# Two concurrent runs on an 8-core instance. Each one's heavy rules ask for all
# of the cores, so this is already an oversubscription -- but a measured one:
# the rules spend much of their time off-CPU, and holding the limit at 1 would
# idle the box whenever the single run is in an I/O or single-threaded stage.
MAX_CONCURRENT_RUNS = int(os.environ.get("PRIMERCAST_MAX_CONCURRENT_RUNS", "2"))

# A queued session re-polls every couple of seconds. Give it room for a slow
# rerun before assuming the browser is gone.
WAITER_TTL = 30.0

_lock = threading.Lock()
_tickets = itertools.count()
_waiting: list[int] = []            # FIFO of queued ticket ids
_running: set[int] = set()          # tickets holding a slot
_last_seen: dict[int, float] = {}   # ticket -> last try_start() call


def _prune_locked() -> None:
    """Drop queued tickets whose session stopped polling."""
    cutoff = time.monotonic() - WAITER_TTL
    for ticket in [t for t in _waiting if _last_seen.get(t, 0.0) < cutoff]:
        _waiting.remove(ticket)
        _last_seen.pop(ticket, None)


def take_ticket() -> int:
    """Join the queue. Pair every ticket with a ``release`` in a finally."""
    with _lock:
        ticket = next(_tickets)
        _waiting.append(ticket)
        _last_seen[ticket] = time.monotonic()
        return ticket


def try_start(ticket: int) -> tuple[bool, int]:
    """Claim a slot if one is free and this ticket is first in line.

    Returns ``(admitted, runs_ahead)``. Also refreshes the ticket's heartbeat,
    so a queued session must keep calling this to hold its place.
    """
    with _lock:
        # Prune on every poll, not just a waiter's, so one stale ticket at the
        # head of the queue cannot hold up admission until someone behind it
        # happens to call in.
        _prune_locked()
        if ticket in _running:
            return True, 0
        _last_seen[ticket] = time.monotonic()
        if ticket not in _waiting:
            # Unknown ticket: its session went quiet long enough to be pruned
            # (or released) and has now come back. Re-join at the back rather
            # than polling forever against a queue it is no longer in.
            _waiting.append(ticket)
        ahead = _waiting.index(ticket)
        # Admit anyone who fits in the free slots, not just the head of the
        # queue: a burst of sessions would otherwise take one poll interval per
        # slot to fill the instance. Still strictly FIFO -- `ahead` sessions
        # queued earlier are admitted in the same pass.
        if ahead < MAX_CONCURRENT_RUNS - len(_running):
            _waiting.remove(ticket)
            _last_seen.pop(ticket, None)
            _running.add(ticket)
            return True, 0
        return False, ahead


def release(ticket: int) -> None:
    """Give up a slot or a queue place. Idempotent.

    Both the run's ``finally`` and the Stop button can reach this for the same
    ticket, so releasing twice must not free a slot that was never held.
    """
    with _lock:
        if ticket in _waiting:
            _waiting.remove(ticket)
        _last_seen.pop(ticket, None)
        _running.discard(ticket)


def stats() -> tuple[int, int]:
    """(running, waiting) -- for diagnostics and tests."""
    with _lock:
        return len(_running), len(_waiting)


def _reset_for_tests() -> None:
    with _lock:
        _waiting.clear()
        _running.clear()
        _last_seen.clear()
