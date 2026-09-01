"""Regression: run-263 incident (t_7f85aa6f) — an unaffiliated caller
(e.g. an interactive desktop session with no HERMES_KANBAN_* env) could
``complete_task`` a card that was ``running`` under a dispatcher worker's
live claim. The completion cleared the worker's claim, stamped the
foreign caller's summary onto the worker's run row (attribution hijack),
and marked the task ``done`` while the real worker was mid-run (its own
completion then failed as "already terminal").

Mirror of the M1 guard in ``request_review`` (force=True override) and
the ``expected_run_id`` CAS in ``block_task``: ``complete_task`` is the
last terminal transition that accepted un-owned callers on a live run.
"""

from __future__ import annotations

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


def _claimed_task(conn, claimer="worker-a"):
    tid = kb.create_task(conn, title="t", assignee="worker-a")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer=claimer)
    assert claimed is not None
    return tid


def _run_row(conn, tid):
    return conn.execute(
        "SELECT id, status, outcome, ended_at, summary FROM task_runs "
        "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Guard: un-owned completion of a live run must be refused, atomically
# ---------------------------------------------------------------------------

def test_unowned_complete_on_live_claim_is_refused(kanban_home):
    conn = kb.connect()
    try:
        tid = _claimed_task(conn)  # running, live claim_lock, current_run_id
        with pytest.raises(kb.ClaimOwnershipError) as excinfo:
            kb.complete_task(conn, tid, summary="interactive twin hijack")
        assert "expected_run_id" in str(excinfo.value)
        # Nothing mutated: task still running under the worker's claim.
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.claim_lock is not None
        run = _run_row(conn, tid)
        assert run["status"] == "running" and run["ended_at"] is None
        assert run["summary"] is None
        # Audit event landed.
        ev = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? "
            "ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
        assert ev["kind"] == "completion_refused_claim_ownership"
    finally:
        conn.close()


def test_owner_complete_with_expected_run_id_still_works(kanban_home):
    conn = kb.connect()
    try:
        tid = _claimed_task(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.complete_task(
            conn, tid, summary="done", expected_run_id=run_id
        ) is True
        task = kb.get_task(conn, tid)
        assert task.status == "done" and task.claim_lock is None
        run = _run_row(conn, tid)
        assert run["status"] == "done" and run["outcome"] == "completed"
        assert run["summary"] == "done"
    finally:
        conn.close()


def test_force_override_completes_live_run(kanban_home):
    """Human/CLI override — mirrors force=True on request_review (M1)."""
    conn = kb.connect()
    try:
        tid = _claimed_task(conn)
        assert kb.complete_task(
            conn, tid, summary="operator override", force=True
        ) is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_wrong_run_id_still_fails_cleanly(kanban_home):
    conn = kb.connect()
    try:
        tid = _claimed_task(conn)
        assert kb.complete_task(
            conn, tid, summary="stale worker", expected_run_id=999999
        ) is False
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unchanged legacy paths (no live claim → no guard)
# ---------------------------------------------------------------------------

def test_complete_ready_task_without_claim_still_works(kanban_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="w")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.complete_task(conn, tid, summary="cli done") is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_complete_blocked_task_without_live_claim_still_works(kanban_home):
    conn = kb.connect()
    try:
        tid = _claimed_task(conn)
        assert kb.block_task(conn, tid, reason="waiting on human") is True
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.complete_task(conn, tid, summary="human closed it") is True
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()
