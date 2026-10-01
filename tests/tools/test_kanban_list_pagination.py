"""Regression tests for kanban_list pagination integrity (card t_7264b7fc).

Background (2026-10-01 forensics, debugger): the Daily Fleet Optimizer filed a
defect claiming assignee-filtered done views cap at exactly 100 rows with
``truncated: false`` even at ``limit=200``. Forensics on the primary artifacts
(default home state.db transcripts + board DB) showed the tool was NEVER broken:

- Sep 23..Sep 30 the board genuinely held exactly 100 done cards for ``frontend``
  (no frontend completion between Sep-22 22:52 and Sep-30 13:34 PDT), so
  ``count=100, truncated=false`` was the TRUE answer each day — the byte-identical
  payloads were identical because the data and args were identical, not because
  of a cache or cap.
- On Oct-01 the same call (assignee=frontend status=done limit=200) returned
  ``count=116`` with all 116 ids (transcript msgs 39252/39295 + spillover file
  both carry 116), and the live handler reproduces count=116 on a scratch copy
  of the real board.

The defect class to guard against is nonetheless real: a silent cap or an
untruthful ``truncated`` flag on per-assignee done views would hide older cards
from every audit (the optimizer's step-7a full-window sweeps). These tests pin
the honest behavior at the >100 boundary the card was filed about: >100 done
cards for ONE assignee must all come back at ``limit=200`` with
``truncated: false``, and the truncation flag must flip truthfully when the
window is smaller than the set.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def orchestrator_env(monkeypatch, tmp_path):
    """Isolated HERMES_HOME with a fresh board; NO HERMES_KANBAN_TASK so the
    caller counts as an orchestrator (kanban_list is orchestrator-only)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _seed_done(conn, kb, assignee: str, n: int, *, start_priority: int = 5) -> list[str]:
    """Create ``n`` tasks for ``assignee`` and drive them to done; returns ids."""
    ids = []
    for i in range(n):
        tid = kb.create_task(conn, title=f"bulk-{i}", assignee=assignee,
                             priority=start_priority)
        kb.complete_task(conn, tid, result=f"done {i}")
        ids.append(tid)
    return ids


def test_list_returns_over_100_done_rows_for_one_assignee(orchestrator_env):
    """>100 done cards for a single assignee: limit=200 must return ALL of them
    (count > 100, nothing hidden) with truncated=false — the exact scenario the
    t_7264b7fc defect card feared was silently capped at 100."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    try:
        ids = _seed_done(conn, kb, "frontend", 116)
    finally:
        conn.close()

    out = kt._handle_list({"assignee": "frontend", "status": "done", "limit": 200})
    d = json.loads(out)
    got = [t["id"] for t in d["tasks"]]
    assert d["count"] == 116, f"expected all 116 done cards, got {d['count']}"
    assert d["limit"] == 200
    assert d["truncated"] is False
    assert sorted(got) == sorted(ids), "some done cards are missing from the listing"


def test_list_reports_truncation_truthfully_when_window_smaller_than_set(orchestrator_env):
    """Same 116-card set through a 100-row window: exactly 100 rows must come
    back AND truncated must be true with a next_limit hint — a silent 100-row
    cap that reported truncated=false was the card's failure signature."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    try:
        _seed_done(conn, kb, "frontend", 116)
    finally:
        conn.close()

    d = json.loads(kt._handle_list({"assignee": "frontend", "status": "done", "limit": 100}))
    assert d["count"] == 100
    assert len(d["tasks"]) == 100
    assert d["truncated"] is True, "a 100-row window over 116 cards MUST report truncation"
    assert d["next_limit"] == 200, "next_limit should double up to the 200 max"


def test_list_unfiltered_done_view_not_capped_by_assignee_partition(orchestrator_env):
    """The unfiltered done view must see the same cards the per-assignee view
    sees (the card's fact #2 claimed a divergence). 116 for one assignee plus a
    handful elsewhere: both views reconcile to the same totals."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    try:
        fe_ids = _seed_done(conn, kb, "frontend", 116)
        other_ids = _seed_done(conn, kb, "cmo", 7, start_priority=3)
    finally:
        conn.close()

    per_assignee = json.loads(kt._handle_list({"assignee": "frontend", "status": "done", "limit": 200}))
    unfiltered = json.loads(kt._handle_list({"status": "done", "limit": 200}))
    assert per_assignee["count"] == 116
    assert {t["id"] for t in unfiltered["tasks"]} >= set(fe_ids), (
        "unfiltered done view is missing cards the assignee view returns"
    )
    assert {t["id"] for t in unfiltered["tasks"]} >= set(other_ids)
    assert unfiltered["count"] == 123
    assert unfiltered["truncated"] is False
