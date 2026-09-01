"""Dispatcher progress-staleness watchdog (run-244 wedge, second half).

Evidence (debugger t_3df0dd33, 2026-09-01): run 244 on t_a617051b polled a
hung background process with ``process(action="wait")`` 76 times over ~3.9h.
Once half 1 (progress-gated heartbeat bridge, tests/tools/
test_kanban_progress_heartbeat.py) lands, those identical polls stop bumping
``last_heartbeat_at`` - so ``detect_stale_running`` (1h gap threshold) WOULD
eventually catch it. But a wedge can also present as a *fresh heartbeat with
no progress* (e.g. a worker repeatedly polling a no-op result while an
auto-reconnect loop keeps activity stamps alive), and the gap-only check has
no signal for that.

This file pins the second half of the fix:

1. ``heartbeat_worker(progress=True)`` records a durable ``last_progress_at``
   column alongside ``last_heartbeat_at``.
2. ``detect_progress_stalled`` (sibling of ``detect_stale_running``) reclaims
   a ``running`` task whose heartbeat cadence is ALIVE but whose progress is
   stale beyond the configured threshold (default conservative: 30 min).
   Config-gated via ``kanban.progress_watchdog.*``; disabled unless enabled.
3. A wired integration test simulating the run-244 sequence (identical
   process-wait polls + passive stream waits, no assistant text) asserts the
   board now reclaims within minutes of the progress threshold, not hours.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture()
def conn(tmp_path: Path):
    db = kb.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _claimed(conn, title="wedge sim"):
    task_id = kb.create_task(conn, title=title, assignee="backend")
    claimed = kb.claim_task(conn, task_id, claimer="tester:1")
    assert claimed is not None
    return task_id


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_tasks_have_last_progress_at(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "last_progress_at" in cols


# ---------------------------------------------------------------------------
# heartbeat_worker progress recording
# ---------------------------------------------------------------------------

def test_progress_heartbeat_sets_last_progress_at(conn):
    task_id = _claimed(conn)
    assert kb.heartbeat_worker(conn, task_id, progress=True)
    row = conn.execute(
        "SELECT last_heartbeat_at, last_progress_at FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["last_heartbeat_at"] is not None
    assert row["last_progress_at"] is not None


def test_passive_heartbeat_does_not_set_last_progress_at(conn):
    task_id = _claimed(conn)
    assert kb.heartbeat_worker(conn, task_id)  # no progress flag
    row = conn.execute(
        "SELECT last_heartbeat_at, last_progress_at FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["last_heartbeat_at"] is not None
    assert row["last_progress_at"] is None


def test_progress_and_passive_heartbeats_diverge(conn):
    task_id = _claimed(conn)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET last_progress_at = ? WHERE id = ?",
            (int(time.time()) - 3600, task_id),
        )
    assert kb.heartbeat_worker(conn, task_id)  # passive refresh
    row = conn.execute(
        "SELECT last_heartbeat_at, last_progress_at FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    # Heartbeat is fresh, progress is an hour old -> the divergence signature.
    assert row["last_heartbeat_at"] >= int(time.time()) - 5
    assert row["last_progress_at"] == int(time.time()) - 3600


# ---------------------------------------------------------------------------
# detect_progress_stalled
# ---------------------------------------------------------------------------

def test_detect_progress_stalled_reclaims_alive_heartbeat_stale_progress(conn):
    task_id = _claimed(conn)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = ?, "
            "last_progress_at = ? WHERE id = ?",
            (now - 4000, now - 60, now - 3600, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (now - 4000, task_id),
        )
    reclaimed = kb.detect_progress_stalled(conn, progress_stale_seconds=1800)
    assert reclaimed == [task_id]
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status != "running"
    events = kb.list_events(conn, task_id=task_id)
    kinds = [e.kind for e in events]
    assert "progress_stalled" in kinds
    payload = [e for e in events if e.kind == "progress_stalled"][-1].payload
    assert payload is not None
    assert payload.get("last_progress_at") is not None
    assert payload.get("last_heartbeat_at") is not None


def test_detect_progress_stalled_skips_recent_tasks_by_default_min_runtime(conn):
    """Default min_runtime (600s) protects young tasks: a slow first LLM
    call is not a wedge."""
    task_id = _claimed(conn)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = ?, "
            "last_progress_at = ? WHERE id = ?",
            (now - 1200, now - 60, now - 1500, task_id),
        )
    # stale by 1800 threshold? progress is 1500s old -> NOT yet stale at 1800.
    assert kb.detect_progress_stalled(conn, progress_stale_seconds=1800) == []


def test_detect_progress_stalled_skips_fresh_progress(conn):
    task_id = _claimed(conn)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = ?, "
            "last_progress_at = ? WHERE id = ?",
            (now - 4000, now - 60, now - 60, task_id),
        )
    assert kb.detect_progress_stalled(conn, progress_stale_seconds=1800) == []


def test_detect_progress_stalled_requires_min_runtime(conn):
    """A task younger than ``min_runtime_seconds`` is never flagged - a slow
    first LLM call is not a wedge."""
    task_id = _claimed(conn)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = ?, "
            "last_progress_at = ? WHERE id = ?",
            (now - 300, now - 60, now - 3600, task_id),
        )
    assert kb.detect_progress_stalled(
        conn, progress_stale_seconds=1800, min_runtime_seconds=3600
    ) == []


def test_detect_progress_stalled_needs_progress_seen_at_least_once(conn):
    """NULL progress (worker pre-half-1 or long tool call) is not flagged by
    the progress check; the gap-based detect_stale_running owns that case."""
    task_id = _claimed(conn)
    now = int(time.time())
    heartbeat_only = now - 60
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = ?, "
            "last_progress_at = NULL WHERE id = ?",
            (now - 4000, heartbeat_only, task_id),
        )
    assert kb.detect_progress_stalled(conn, progress_stale_seconds=1800) == []


def test_detect_progress_stalled_off_when_config_disabled(conn):
    """progress_stale_seconds=0 (config disabled) -> no-op."""
    task_id = _claimed(conn)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (now - 4000, task_id),
        )
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = ?, "
            "last_progress_at = ? WHERE id = ?",
            (now - 4000, now - 60, now - 3600, task_id),
        )
    assert kb.detect_progress_stalled(conn, progress_stale_seconds=0) == []


def test_detect_progress_stalled_no_progress_column_noop(conn):
    """Legacy boards without the column: fail-open no-op, no exception."""
    task_id = _claimed(conn)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute("ALTER TABLE tasks DROP COLUMN last_progress_at")
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (now - 4000, now - 60, task_id),
        )
    assert kb.detect_progress_stalled(conn, progress_stale_seconds=1800) == []


# ---------------------------------------------------------------------------
# dispatch_once wiring
# ---------------------------------------------------------------------------

def test_dispatch_once_runs_progress_watchdog(conn, monkeypatch):
    task_id = _claimed(conn)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_heartbeat_at = ?, "
            "last_progress_at = ? WHERE id = ?",
            (now - 4000, now - 60, now - 3600, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (now - 4000, task_id),
        )
    seen: list[int] = []
    real = kb.detect_progress_stalled

    def spy(c, **kw):
        seen.append(1)
        return real(c, **kw)

    monkeypatch.setattr(kb, "detect_progress_stalled", spy)
    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *a, **k: None,
        progress_stale_seconds=1800,
        progress_min_runtime_seconds=600,
    )
    assert seen, "dispatch_once must invoke detect_progress_stalled"
    assert result.progress_stalled == [task_id]


# ---------------------------------------------------------------------------
# run-244 wired integration: identical polls + passive waits, no text
# ---------------------------------------------------------------------------

def test_run244_sequence_reclaims_within_minutes(conn):
    """Simulate the exact run-244 sequence with the post-fix semantics:

    - worker polls the same ``process(action="wait", session_id=X)`` with
      drifting timeout numerics - after half 1 these are NOT progress, so
      the bridge stops writing progress heartbeats;
    - passive stream/wait ticks keep ``last_heartbeat_at`` fresh (claim TTL
      survives);
    - assistant text is empty every turn (68/68 in the incident);
    - after the 30-min progress-staleness threshold, the dispatcher must
      reclaim - minutes after the threshold, not 4 hours.
    """
    task_id = _claimed(conn, title="run-244 wedge")
    sim_now = int(time.time()) - 4000
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ?, last_progress_at = ? WHERE id = ?",
            (sim_now, sim_now, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (sim_now, task_id),
        )

    # Incident cadence: ~45 min of passive-only heartbeats (identical wait
    # polls + stream/wait ticks; zero progress signals after half 1).
    for _ in range(45):
        sim_now += 60
        kb.heartbeat_worker(conn, task_id)  # passive (no progress flag)

    reclaimed = kb.detect_progress_stalled(conn, progress_stale_seconds=1800)
    assert reclaimed == [task_id], (
        "run-244 sequence (alive heartbeats + zero progress for >30min) "
        "must be reclaimed by the progress watchdog"
    )


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

def test_progress_watchdog_config_defaults():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    kanban = DEFAULT_CONFIG.get("kanban", {})
    pw = kanban.get("progress_watchdog", {})
    assert pw.get("enabled") is False
    assert pw.get("progress_stale_seconds") == 1800
    assert pw.get("min_runtime_seconds") == 600
