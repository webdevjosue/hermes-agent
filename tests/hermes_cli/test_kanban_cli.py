"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------



def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert f"Task {child_id}: child task" in output
    assert f"parents:   {parent_id}" in output
    assert "Cannot operate on a closed database" not in output


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban lexer — Windows absolute paths must survive tokenization (t_60d3fa15)
# ---------------------------------------------------------------------------
#
# POSIX shlex.split() treats every backslash as an escape, silently turning
# `C:\Users\me\upload.txt` into `C:Usersmeupload.txt` before the attach
# subcommand's Path() ever sees it. run_slash must keep backslashes verbatim
# while preserving "..." / '...' grouping.


def test_run_slash_preserves_windows_absolute_paths(kanban_home):
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="win-path lexer", assignee="tester")

    src_dir = kanban_home / "uploads"
    src_dir.mkdir()
    src = src_dir / "upload.txt"
    src.write_bytes(b"windows path body")

    # Unquoted Windows abs path — the exact failure shape from the bug report.
    out = kc.run_slash(f"attach {task_id} {src}")
    assert "Attached" in out, out

    with kb.connect() as conn:
        atts = kb.list_attachments(conn, task_id)
        assert len(atts) == 1
        assert atts[0].filename == "upload.txt"
        assert Path(atts[0].stored_path).read_bytes() == b"windows path body"


def test_run_slash_lexer_windows_paths_and_quoting():
    """Unit-level: _split_slash_args keeps backslashes and quote grouping."""
    split = kc._split_slash_args

    # Bare Windows abs path token: backslashes must survive verbatim.
    assert split(r"attach t_x C:\Users\Josue\upload.txt") == [
        "attach", "t_x", r"C:\Users\Josue\upload.txt",
    ]

    # Quoted path with spaces still groups into a single token, quotes
    # stripped, backslashes preserved.
    assert split('attach t_x "C:\\Program Files\\my file.txt"') == [
        "attach", "t_x", r"C:\Program Files\my file.txt",
    ]

    # Single quotes group too.
    assert split("comment t_x 'hello world'") == [
        "comment", "t_x", "hello world",
    ]

    # Whitespace splitting on unquoted spaces.
    assert split("list --json") == ["list", "--json"]

    # Blank / empty input yields no tokens (bare /kanban help path).
    assert split("") == []
    assert split("   ") == []

    # Unbalanced quotes still raise, same as shlex.split.
    with pytest.raises(ValueError):
        split('attach t_x "unbalanced')


