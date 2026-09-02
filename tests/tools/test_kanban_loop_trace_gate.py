"""Tests for the loop-closure trace completion gate
(cards t_31b252aa + t_a617051b; LOOP-CLOSURE LAW enforcement,
tools/kanban_tools.py).

The gate lives in ``_missing_loop_trace`` + the call inside
``_handle_complete``.  It is scoped to profiles listed in
``_LOOP_TRACE_REQUIRED_PROFILES`` (orchestrator "default"; frontend
added as pilot #2 by card t_a617051b after the 2026-09-01 Fleet Loop
Digest showed prompt-only LAW text at 2/7 compliance vs 3/3 for the
code-gated orchestrator) and rejects completions whose summary carries
neither the four trace verbs (attempted / failed / verified /
do-differently) nor the explicit "loop-closure: nothing new" escape
hatch.
"""
from __future__ import annotations

import json

import pytest


TRACE = (
    "shipped the gate. "
    "attempted: code gate + tests. "
    "failed: none. "
    "verified: pytest green. "
    "do-differently: n/a."
)


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Local copy of tests/tools/test_kanban_tools.py::worker_env —
    isolated HERMES_HOME + a claimed task bound to HERMES_KANBAN_TASK."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="loop-trace-gate-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    # Owner contract for the live-claim guard (run-263, t_7f85aa6f):
    # pin the run id the claim created, same as the canonical
    # worker_env in tests/tools/test_kanban_tools.py.
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.id))
    return tid


@pytest.fixture
def default_profile_env(worker_env, monkeypatch):
    """worker_env but acting as the default (orchestrator) profile."""
    monkeypatch.setenv("HERMES_PROFILE", "default")
    return worker_env


@pytest.fixture
def frontend_profile_env(worker_env, monkeypatch):
    """worker_env but acting as the frontend profile (pilot #2,
    card t_a617051b)."""
    monkeypatch.setenv("HERMES_PROFILE", "frontend")
    return worker_env


def _out_dict(out: str) -> dict:
    return json.loads(out)


# ---------------------------------------------------------------------------
# Unit: _missing_loop_trace
# ---------------------------------------------------------------------------

# Gated profiles: the orchestrator (t_31b252aa) and frontend as
# pilot #2 (t_a617051b, Fleet Loop Digest 2026-09-01 Daily: default
# code-gated 3/3 traces vs frontend prompt-only 2/7).
_GATED_PROFILES = ("default", "frontend")

# A profile that is deliberately NOT in _LOOP_TRACE_REQUIRED_PROFILES.
# "frontend" was the original example here but became gated by
# t_a617051b, so the exemption case now uses an ungated worker.
_UNGATED_PROFILE = "test-worker"


class TestMissingLoopTraceUnit:
    def test_other_profiles_exempt(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", _UNGATED_PROFILE)
        assert kt._missing_loop_trace("did stuff") is None

    @pytest.mark.parametrize("profile", _GATED_PROFILES)
    def test_gated_traceless_rejected(self, monkeypatch, profile):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", profile)
        msg = kt._missing_loop_trace("did stuff, all good")
        assert msg is not None and "attempted" in msg

    @pytest.mark.parametrize("profile", _GATED_PROFILES)
    def test_gated_full_trace_passes(self, monkeypatch, profile):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", profile)
        assert kt._missing_loop_trace(TRACE) is None

    @pytest.mark.parametrize("profile", _GATED_PROFILES)
    def test_gated_partial_trace_rejected(self, monkeypatch, profile):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", profile)
        # missing 'do-differently'
        partial = "attempted x. failed y. verified z."
        msg = kt._missing_loop_trace(partial)
        assert msg is not None and "do-differently" in msg

    @pytest.mark.parametrize("profile", _GATED_PROFILES)
    def test_gated_nothing_new_escape_hatch(self, monkeypatch, profile):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", profile)
        assert kt._missing_loop_trace("ran probe. loop-closure: nothing new") is None

    def test_default_traceless_rejected(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "default")
        msg = kt._missing_loop_trace("did stuff, all good")
        assert msg is not None and "attempted" in msg

    def test_default_full_trace_passes(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "default")
        assert kt._missing_loop_trace(TRACE) is None

    def test_default_partial_trace_rejected(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "default")
        # missing 'do-differently'
        partial = "attempted x. failed y. verified z."
        msg = kt._missing_loop_trace(partial)
        assert msg is not None and "do-differently" in msg

    def test_nothing_new_escape_hatch(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "default")
        assert kt._missing_loop_trace("ran probe. loop-closure: nothing new") is None

    def test_case_insensitive_verbs(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "default")
        upper = TRACE.upper()
        assert kt._missing_loop_trace(upper) is None

    def test_unprofiled_env_falls_back_to_default(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        # fallback resolves to "default" -> gated
        msg = kt._missing_loop_trace("plain summary")
        assert msg is not None


# ---------------------------------------------------------------------------
# Integration: _handle_complete honours the gate
# ---------------------------------------------------------------------------

class TestCompleteGateIntegration:
    def test_default_traceless_completion_rejected(self, default_profile_env):
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": "all done, shipped it"})
        d = _out_dict(out)
        assert "error" in d
        assert "LOOP-CLOSURE" in d["error"]
        # task must still be in-flight (not done)
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            t = kb.get_task(conn, default_profile_env)
            assert t.status == "running", (
                f"gate must leave task in-flight, got {t.status}"
            )
        finally:
            conn.close()

    def test_default_traced_completion_succeeds(self, default_profile_env):
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": TRACE})
        d = _out_dict(out)
        assert d.get("ok") is True, d
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            t = kb.get_task(conn, default_profile_env)
            assert t.status == "done"
        finally:
            conn.close()

    def test_default_nothing_new_completion_succeeds(self, default_profile_env):
        from tools import kanban_tools as kt
        out = kt._handle_complete({
            "summary": "probe re-run. loop-closure: nothing new"
        })
        d = _out_dict(out)
        assert d.get("ok") is True, d

    def test_worker_profile_unaffected(self, worker_env):
        """worker_env sets HERMES_PROFILE=test-worker — no gate."""
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": "plain worker summary"})
        d = _out_dict(out)
        assert d.get("ok") is True, d

    def test_frontend_traceless_completion_rejected(self, frontend_profile_env):
        """Pilot #2 (t_a617051b): frontend completions need the trace."""
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": "shipped the section, looks great"})
        d = _out_dict(out)
        assert "error" in d
        assert "LOOP-CLOSURE" in d["error"]
        # rejection must state what is missing so the worker can retry
        assert "missing" in d["error"]
        # task must still be in-flight (gate fires before DB mutation)
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            t = kb.get_task(conn, frontend_profile_env)
            assert t.status == "running", (
                f"gate must leave task in-flight, got {t.status}"
            )
        finally:
            conn.close()

    def test_frontend_traced_completion_succeeds(self, frontend_profile_env):
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": TRACE})
        d = _out_dict(out)
        assert d.get("ok") is True, d
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            t = kb.get_task(conn, frontend_profile_env)
            assert t.status == "done"
        finally:
            conn.close()

    def test_frontend_nothing_new_completion_succeeds(self, frontend_profile_env):
        from tools import kanban_tools as kt
        out = kt._handle_complete({
            "summary": "css re-check. loop-closure: nothing new"
        })
        d = _out_dict(out)
        assert d.get("ok") is True, d
