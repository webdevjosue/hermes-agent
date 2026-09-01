"""Progress-gated runtime-activity -> board-heartbeat bridge (run-244 wedge).

Evidence (debugger t_3df0dd33, run 244 on t_a617051b, 2026-09-01): during a
4-hour wedge the task received 217 board heartbeats at 60s median - but only
ONE was model-sent. The rest came from the auto-heartbeat bridge
(``heartbeat_current_worker_from_env`` -> ``heartbeat_worker``), which ticked
on EVERY ``_touch_activity`` stamp - including passive
"waiting for provider response" / "receiving stream response" waits emitted
by ``_emit_wait_notice`` during a blocking process-wait - rate-limited only
to one DB write / 60s. A wedged worker emitting bare identical tool calls
every ~3min was therefore indistinguishable from a healthy one: the wedge
was SELF-SUSTAINING liveness cover for ``detect_stale_running``.

New contract pinned here:

1. ``mark_board_progress`` (tools/kanban_tools.py) is the single gated entry
   point. The board heartbeat write fires ONLY on PROGRESS signals - a new
   tool name, a new normalized-args signature, non-empty (changed) assistant
   text, an explicit ``kanban_heartbeat``, or an activity stamp carrying the
   ``AGENT_PROGRESS`` provenance - never on passive wait/stream ticks alone.
2. Arg normalization drops volatile wait-shaping numerics (timeout et al.)
   by reusing ``RepetitionWatchdogState.signature`` (static + lock-free; the
   shared streak state is NOT touched, per the t_5b9c8c40 review caveat the
   in-worker ``observe()`` stays unguarded - this module never reads it).
3. Passive ticks still refresh the claim TTL (``heartbeat_claim``) so a
   genuinely-alive worker inside one long tool call is not reclaimed by
   ``release_stale_claims`` before the progress watchdog's conservative
   threshold (the #23025 trap the bridge was originally built to solve).
4. Fail-open / best-effort semantics preserved: nothing here may raise into
   the agent loop.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tools.kanban_tools as kt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeKb:
    """In-memory stand-in for the hermes_cli.kanban_db surface the bridge
    uses (``heartbeat_claim`` / ``heartbeat_worker``)."""

    def __init__(self) -> None:
        self.claim_calls: list[dict] = []
        self.worker_calls: list[dict] = []

    def heartbeat_claim(self, conn, task_id, *, claimer=None, ttl_seconds=None):
        self.claim_calls.append({"task_id": task_id, "claimer": claimer})
        return True

    def heartbeat_worker(self, conn, task_id, *, note=None, expected_run_id=None,
                         progress=None):
        self.worker_calls.append(
            {
                "task_id": task_id,
                "note": note,
                "expected_run_id": expected_run_id,
                "progress": progress,
            }
        )
        return True


@pytest.fixture()
def worker_env(monkeypatch):
    fake_kb = _FakeKb()
    fake_conn = MagicMock()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_progress_gate")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host-1:100")
    monkeypatch.setattr(kt, "_connect", lambda board=None: (fake_kb, fake_conn))
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(kt, "_now_monotonic", lambda: clock["t"])

    def advance(seconds: float) -> None:
        clock["t"] += seconds

    return SimpleNamespace(fake_kb=fake_kb, advance=advance)


def _reset_bridge_state() -> None:
    kt._auto_heartbeat_last_attempt = 0.0
    kt._progress_last_tool_sig = None
    kt._progress_last_text_hash = None


# ---------------------------------------------------------------------------
# Half 1: progress-only board heartbeat
# ---------------------------------------------------------------------------

def test_passive_wait_touches_do_not_write_board_heartbeat(worker_env):
    """RED: N wait-notice touches with no tool/text change -> no board write.

    This is the run-244 signature: the bridge ticked 217 times on passive
    stream/wait activity and masked the stall from ``detect_stale_running``.
    """
    _reset_bridge_state()

    for _ in range(60):
        kt.mark_board_progress(
            kind="activity",
            desc="waiting on the provider - 180s with no output yet",
        )
        worker_env.advance(65.0)

    assert worker_env.fake_kb.worker_calls == [], (
        "passive wait ticks must not bump the board heartbeat - that is the "
        "self-sustaining liveness cover from run-244"
    )


def test_new_tool_name_is_progress(worker_env):
    _reset_bridge_state()

    kt.mark_board_progress(kind="tool", tool_name="read_file", args={"path": "a.py"})
    assert len(worker_env.fake_kb.worker_calls) == 1
    assert worker_env.fake_kb.worker_calls[0]["progress"] is True

    # Same tool + same args signature: not progress.
    worker_env.fake_kb.worker_calls.clear()
    worker_env.advance(120.0)
    kt.mark_board_progress(kind="tool", tool_name="read_file", args={"path": "a.py"})
    assert worker_env.fake_kb.worker_calls == []


def test_new_args_signature_is_progress(worker_env):
    _reset_bridge_state()

    kt.mark_board_progress(kind="tool", tool_name="read_file", args={"path": "a.py"})
    assert len(worker_env.fake_kb.worker_calls) == 1

    # Different file -> different normalized args -> progress.
    worker_env.advance(120.0)
    kt.mark_board_progress(kind="tool", tool_name="read_file", args={"path": "b.py"})
    assert len(worker_env.fake_kb.worker_calls) == 2


def test_volatile_numeric_drift_is_not_progress(worker_env):
    """run-244: timeout drifted 170->175->178 - that must NOT count as new
    progress. Reuses the repetition watchdog's VOLATILE_ARG_KEYS set."""
    _reset_bridge_state()

    kt.mark_board_progress(
        kind="tool", tool_name="process",
        args={"action": "wait", "session_id": "s1", "timeout": 170},
    )
    assert len(worker_env.fake_kb.worker_calls) == 1

    # timeout drift only: same normalized signature, no progress write.
    worker_env.advance(120.0)
    kt.mark_board_progress(
        kind="tool", tool_name="process",
        args={"action": "wait", "session_id": "s1", "timeout": 178},
    )
    assert len(worker_env.fake_kb.worker_calls) == 1, (
        "timeout drift is not progress - that is the exact run-244 poll loop"
    )

    # ``action`` is NOT volatile - a different action is a different call.
    worker_env.advance(120.0)
    kt.mark_board_progress(
        kind="tool", tool_name="process",
        args={"action": "run", "session_id": "s1", "timeout": 178},
    )
    assert len(worker_env.fake_kb.worker_calls) == 2


def test_signature_state_updates_even_when_rate_limited(worker_env):
    """A busy batch fires many signals inside one 60s window; the signature
    tracker must still advance so the NEXT distinct call after the window is
    recognized (and an identical re-issue is not)."""
    _reset_bridge_state()

    kt.mark_board_progress(kind="tool", tool_name="read_file", args={"path": "a.py"})
    # Within the rate window: no write, but state advances.
    kt.mark_board_progress(kind="tool", tool_name="read_file", args={"path": "b.py"})
    assert len(worker_env.fake_kb.worker_calls) == 1

    # Past the window, re-issue of b.py (already observed): NOT progress.
    worker_env.advance(120.0)
    kt.mark_board_progress(kind="tool", tool_name="read_file", args={"path": "b.py"})
    assert len(worker_env.fake_kb.worker_calls) == 1


def test_nonempty_assistant_text_is_progress(worker_env):
    _reset_bridge_state()

    kt.mark_board_progress(kind="text", desc="Here is my analysis of the failing build.")
    assert len(worker_env.fake_kb.worker_calls) == 1

    # Same text repeated verbatim: not progress.
    worker_env.advance(120.0)
    kt.mark_board_progress(kind="text", desc="Here is my analysis of the failing build.")
    assert len(worker_env.fake_kb.worker_calls) == 1

    # Empty text is never progress.
    worker_env.advance(120.0)
    kt.mark_board_progress(kind="text", desc="   ")
    assert len(worker_env.fake_kb.worker_calls) == 1


def test_explicit_heartbeat_is_always_progress(worker_env):
    _reset_bridge_state()

    kt.mark_board_progress(kind="explicit")
    worker_env.advance(120.0)
    kt.mark_board_progress(kind="explicit")
    assert len(worker_env.fake_kb.worker_calls) == 2
    assert all(c["progress"] is True for c in worker_env.fake_kb.worker_calls)


def test_agent_progress_provenance_activity_is_progress(worker_env):
    """Activity stamps carrying the AGENT_PROGRESS provenance count as
    progress regardless of kind (spec: provenance-based gating at the
    _touch_activity seam)."""
    from agent.session_activity import ActivityProvenance

    _reset_bridge_state()

    kt.mark_board_progress(
        kind="activity",
        desc="whatever",
        provenance=ActivityProvenance.AGENT_PROGRESS,
    )
    assert len(worker_env.fake_kb.worker_calls) == 1
    assert worker_env.fake_kb.worker_calls[0]["progress"] is True


def test_claim_ttl_refresh_survives_for_passive_waits(worker_env):
    """Half the bridge's original purpose: a healthy worker sitting inside
    ONE long tool call (quiet build, slow stream) keeps its claim TTL alive
    via ``heartbeat_claim`` even with no progress signal.

    Without this, progress-gating the claim half would reintroduce the
    #23025 trap the bridge was built to solve (reclaim of a live slow
    worker).
    """
    _reset_bridge_state()

    for _ in range(5):
        kt.mark_board_progress(kind="activity", desc="receiving stream response")
        worker_env.advance(65.0)

    assert len(worker_env.fake_kb.claim_calls) >= 1, (
        "claim TTL must still be refreshed on passive activity so a live "
        "slow worker is never reclaimed by release_stale_claims"
    )
    assert worker_env.fake_kb.worker_calls == [], (
        "no board heartbeat write for the passive ticks themselves"
    )


def test_noop_outside_kanban_worker_context(monkeypatch):
    _reset_bridge_state()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert kt.mark_board_progress(kind="tool", tool_name="x", args={}) is False


def test_bridge_fail_open_on_db_error(worker_env, monkeypatch):
    """Fail-open preserved: a bridge failure must never raise into the loop."""
    _reset_bridge_state()

    def _boom(*_a, **_k):
        raise RuntimeError("board db locked")

    monkeypatch.setattr(worker_env.fake_kb, "heartbeat_worker", _boom)
    monkeypatch.setattr(worker_env.fake_kb, "heartbeat_claim", _boom)
    # Must not raise.
    kt.mark_board_progress(kind="explicit")
    worker_env.advance(120.0)
    kt.mark_board_progress(kind="tool", tool_name="read_file", args={"path": "x"})


# ---------------------------------------------------------------------------
# run_agent seam: _execute_tool_calls reports progress
# ---------------------------------------------------------------------------

def _make_tool_call(name: str, arguments: str):
    func = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(function=func)


def test_execute_tool_calls_marks_progress(monkeypatch):
    """The run_agent seam must stamp a progress signal for the dispatched
    tool batch (name + normalized-args) and for non-empty assistant text."""
    import run_agent

    seen: list[dict] = []
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_seam_test")
    monkeypatch.setattr(
        kt, "mark_board_progress", lambda **kw: seen.append(kw)
    )

    single = SimpleNamespace(
        tool_calls=[_make_tool_call("read_file", '{"path": "a.py"}')],
        content="Let me look at the file.",
    )

    agent = SimpleNamespace(
        _execute_tool_calls_sequential=MagicMock(),
        _execute_tool_calls_concurrent=MagicMock(),
    )
    agent._execute_tool_calls = (
        run_agent.AIAgent._execute_tool_calls.__get__(agent, SimpleNamespace)
    )

    agent._execute_tool_calls(single, [], "task-1")

    kinds = [(k.get("kind"), k.get("tool_name")) for k in seen]
    assert ("text", None) in kinds, "non-empty assistant text must be stamped"
    assert ("tool", "read_file") in kinds, "tool dispatch must be stamped"


def test_execute_tool_calls_progress_stamp_never_breaks_dispatch(monkeypatch):
    """If the progress stamp raises, tool execution must still proceed."""
    import run_agent

    def _boom(**_kw):
        raise RuntimeError("bridge blew up")

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_seam_test")
    monkeypatch.setattr(kt, "mark_board_progress", _boom)

    single = SimpleNamespace(
        tool_calls=[_make_tool_call("terminal", '{"command": "ls"}')],
        content="",
    )
    seq = MagicMock()
    agent = SimpleNamespace(_execute_tool_calls_sequential=seq)
    agent._execute_tool_calls = (
        run_agent.AIAgent._execute_tool_calls.__get__(agent, SimpleNamespace)
    )

    agent._execute_tool_calls(single, [], "task-1")
    seq.assert_called_once()


# ---------------------------------------------------------------------------
# Provenance contract
# ---------------------------------------------------------------------------

def test_activity_provenance_progress_member_exists():
    from agent.session_activity import ActivityProvenance

    assert ActivityProvenance.AGENT_PROGRESS.value == "agent.progress"
    # Existing members untouched.
    assert ActivityProvenance.AGENT_COMPRESSION.value == "agent.compression"
    assert ActivityProvenance.UNKNOWN.value == "unknown"
