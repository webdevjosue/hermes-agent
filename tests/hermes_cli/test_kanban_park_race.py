"""Regression tests for the park race (board card t_849eeac9).

Two "promotable while parked" states, both observed on the live board
(2026-09-04, gateway dispatcher on a 60s tick):

1. ``block_task(kind="dependency")`` on a card whose parents are ALL
   terminal parks it in ``'todo'`` — an immediately promotable state, since
   the ``'todo'`` branch of ``recompute_ready`` exists precisely to promote
   parent-gated cards. The next dispatcher tick claims and spawns a worker
   mid-park while the worker's follow-up ``hermes kanban schedule`` write is
   still in flight (t_9727da9a run 311: dependency_wait 04:20:58 ->
   promoted+claimed 04:21:22 -> scheduled 04:22:26 orphaning the spawned
   run).

2. ``create_task(initial_status="blocked")`` writes a ``created`` event
   only — no ``blocked`` event — so the sticky-block predicate
   (``_has_sticky_block``) cannot see it and ``recompute_ready``'s
   ``blocked`` branch auto-promotes the card the moment its parents
   complete (t_fdf58966 run 314: created blocked 05:40:26 -> promoted
   05:41:00 right after the parent completed -> worker spawned onto a
   "Human: restart the desktop app" card).

Fixes under test:

* ``block_task`` refuses ``kind="dependency"`` when every parent is already
  terminal (nothing left to wait on) and points at the atomic alternative
  (``hermes kanban schedule`` / ``schedule_task`` from running).
* ``recompute_ready`` treats a ``blocked`` card without circuit-breaker
  provenance (most recent of ``blocked``/``unblocked``/``gave_up`` events is
  ``gave_up``) as parked-for-a-human: explicit ``unblock_task`` only.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title: str) -> str:
    """Create a parentless task and drive it to ``running``."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _complete_task(conn, tid: str) -> None:
    """Drive a parentless running task to ``done``.

    Goes through ``complete_task`` so the synchronous ``recompute_ready``
    it fires inside the completion transaction is exercised exactly as the
    live dispatcher's completion path does.
    """
    assert kb.complete_task(
        conn, tid, result="done",
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# 1. The two-step park (block dependency -> schedule) must never be
#    interruptible by a dispatcher promote.
# ---------------------------------------------------------------------------


def test_block_then_schedule_park_survives_dispatcher_ticks(
    kanban_home: Path,
) -> None:
    """Acceptance repro for t_849eeac9.

    A running card with every parent already done is the trap state: the
    historical two-step park first called ``kanban_block(kind=dependency)``
    (landing the card in promotable ``todo``) and only then
    ``hermes kanban schedule``. Any dispatcher tick inside that gap
    promoted, claimed and spawned a worker the schedule write then
    clobbered. The fix refuses the dependency block outright and points at
    the single-write park, which no tick can interrupt.
    """
    with kb.connect_closing() as conn:
        parent = _running_task(conn, title="parent")
        _complete_task(conn, parent)

        child = kb.create_task(
            conn, title="time-gated child", assignee="worker", parents=[parent],
        )
        assert kb.get_task(conn, child).status == "ready"
        assert kb.claim_task(conn, child, claimer="worker") is not None
        run_id = kb.get_task(conn, child).current_run_id

        # Step 1 of the historical park: kanban_block(kind=dependency).
        # With every parent terminal this must be REFUSED with guidance to
        # the atomic alternative (before the fix it succeeded and left the
        # card sitting in 'todo', one tick away from a promote).
        with pytest.raises(ValueError, match="schedule"):
            kb.block_task(
                conn, child,
                reason="Time-gated fire-test: wait for the next window",
                kind="dependency",
                expected_run_id=run_id,
            )
        # Refusal is side-effect free: the run is intact, still running.
        assert kb.get_task(conn, child).status == "running"
        assert kb.get_task(conn, child).current_run_id == run_id

        # Step 2: the atomic park — ONE write, straight from running.
        assert kb.schedule_task(
            conn, child,
            reason="Time-gated fire-test: wait for the next window",
            expected_run_id=run_id,
        )
        assert kb.get_task(conn, child).status == "scheduled"

        # Any number of dispatcher ticks must leave the parked card alone.
        for _ in range(5):
            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, child).status == "scheduled"

        # The time gate lifts only via the explicit unblock, landing ready
        # (parents are done).
        assert kb.unblock_task(conn, child)
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# 2. Created-blocked cards must not auto-promote on parent completion.
# ---------------------------------------------------------------------------


def test_created_blocked_card_not_auto_promoted_on_parent_completion(
    kanban_home: Path,
) -> None:
    """t_fdf58966 run 314: a card created ``initial_status="blocked"`` to
    wait for a human was promoted 34s after creation because its parent had
    just completed. Promotion of a created-blocked card must require an
    explicit unblock."""
    with kb.connect_closing() as conn:
        parent = _running_task(conn, title="parent")
        child = kb.create_task(
            conn,
            title="Human: restart the desktop app",
            assignee="worker",
            parents=[parent],
            initial_status="blocked",
        )
        assert kb.get_task(conn, child).status == "blocked"

        # Parent completion fires recompute_ready synchronously inside
        # complete_task — the exact trigger observed on the live board.
        _complete_task(conn, parent)
        assert kb.get_task(conn, child).status == "blocked"

        # Dispatcher ticks must not promote it either.
        for _ in range(3):
            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, child).status == "blocked"

        # The explicit unblock is the only exit, and it lands ready because
        # the parents are done.
        assert kb.unblock_task(conn, child)
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# 3. Circuit-breaker blocks keep their auto-recovery (regression pin).
# ---------------------------------------------------------------------------


def test_breaker_block_still_auto_recovers_when_budget_allows(
    kanban_home: Path,
) -> None:
    """The provenance gate must not kill circuit-breaker recovery.

    A breaker-tripped card (``gave_up`` event, no worker ``blocked`` event)
    still auto-promotes once the effective failure budget allows it — e.g.
    the operator raised ``kanban.failure_limit`` — exactly as before the
    fix. At the exhausted budget it stays blocked, also as before.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="flapper", assignee="worker")
        now = int(time.time())
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', consecutive_failures=2 "
                "WHERE id=?",
                (tid,),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'gave_up', NULL, ?)",
                (tid, now),
            )

        # Exhausted default budget (failures=2 >= limit=2): stays blocked.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"

        # Raised budget: recovers, as it did before the provenance gate.
        assert kb.recompute_ready(conn, failure_limit=5) == 1
        assert kb.get_task(conn, tid).status == "ready"
