"""Near-identical tool-call watchdog contracts (run-244 wedge class).

Reproduces the debugger t_3df0dd33 evidence at the unit level: a live model
polled ``process(action="wait", session_id=...)`` 76 times, 72/76
byte-identical, the rest differing ONLY in the ``timeout`` numeric
(170→175→178). Assistant text was empty every turn; result-text mitigation
("This is not an error") was ignored 75/76 times; nothing bounded the loop.

Contracts pinned here:

1. Signature normalization: volatile numerics (timeout drift) do NOT break
   the streak; target-selecting args (session_id, path, offset) DO.
2. Escalation: nudge at ``max_streak`` (default 5), second nudge after
   ``nudge_grace`` more, force-end after the nudge budget is exhausted.
3. No false positives: idempotent read repeats are nudged advisory-only at
   streak 5 (spec allows: "idempotent reads ... signature must include
   args"); alternation (poll/log) never accumulates; a different call resets
   the streak; config disabled = never fires.
4. Executor seam: both sequential and concurrent paths observe every call
   through the watchdog (integration contract, mocked dispatch).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.tool_call_repetition_watchdog import (
    REPETITION_WATCHDOG_FINISH_REASON,
    RepetitionWatchdogState,
    WatchdogConfig,
    build_repetition_nudge,
)


# ---------------------------------------------------------------------------
# 1. Signature normalization
# ---------------------------------------------------------------------------
class TestSignatureNormalization:
    def test_timeout_drift_keeps_signature_stable(self):
        """The run-244 premise: 170→175→178 must be ONE streak, not four."""
        wd = RepetitionWatchdogState()
        sigs = {
            wd.signature(
                "process",
                {"action": "wait", "session_id": "proc_cbfe77a69afa", "timeout": t},
            )
            for t in (170, 175, 178, 178, 178)
        }
        assert len(sigs) == 1

    def test_different_session_id_is_a_different_call(self):
        wd = RepetitionWatchdogState()
        sig_a = wd.signature("process", {"action": "wait", "session_id": "aaa", "timeout": 60})
        sig_b = wd.signature("process", {"action": "wait", "session_id": "bbb", "timeout": 60})
        assert sig_a != sig_b

    def test_different_action_is_a_different_call(self):
        wd = RepetitionWatchdogState()
        sig_wait = wd.signature("process", {"action": "wait", "session_id": "aaa"})
        sig_log = wd.signature("process", {"action": "log", "session_id": "aaa"})
        assert sig_wait != sig_log

    def test_different_tool_name_is_a_different_call(self):
        wd = RepetitionWatchdogState()
        assert wd.signature("process", {"action": "wait"}) != wd.signature(
            "terminal", {"action": "wait"}
        )

    def test_paging_numerics_are_kept_distinct(self):
        """offset/start_line select WHAT is read — paging must stay distinct."""
        wd = RepetitionWatchdogState()
        sig_1 = wd.signature("read_file", {"path": "a.py", "offset": 1, "limit": 100})
        sig_2 = wd.signature("read_file", {"path": "a.py", "offset": 101, "limit": 100})
        assert sig_1 != sig_2

    def test_two_different_files_are_distinct_calls(self):
        wd = RepetitionWatchdogState()
        assert wd.signature("read_file", {"path": "a.py"}) != wd.signature(
            "read_file", {"path": "b.py"}
        )

    def test_volatile_keys_dropped_recursively(self):
        wd = RepetitionWatchdogState()
        sig_1 = wd.signature(
            "tool_call", {"name": "x", "arguments": {"session_id": "s", "timeout": 5}}
        )
        sig_2 = wd.signature(
            "tool_call", {"name": "x", "arguments": {"session_id": "s", "timeout": 999}}
        )
        assert sig_1 == sig_2


# ---------------------------------------------------------------------------
# 2. Escalation: nudge → nudge → force_end
# ---------------------------------------------------------------------------
class TestEscalation:
    def _wait_call(self, timeout=178):
        return "process", {
            "action": "wait",
            "session_id": "proc_cbfe77a69afa",
            "timeout": timeout,
        }

    def test_nudge_fires_at_max_streak_despite_timeout_drift(self):
        """The core incident reproduction: streak 5 with drifting timeout."""
        wd = RepetitionWatchdogState()
        actions = []
        for i, t in enumerate([178, 175, 170, 178, 178, 178]):
            decision = wd.observe(*self._wait_call(t))
            actions.append(decision.action)
        # First four below threshold, 5th identical-ish call fires the nudge.
        assert actions == ["none", "none", "none", "none", "nudge", "none"]

    def test_second_nudge_after_grace_then_force_end(self):
        wd = RepetitionWatchdogState()
        seen = []
        # max_streak=5, max_nudges=2, nudge_grace=3 → force_end at streak 11.
        for i in range(1, 12):
            decision = wd.observe(*self._wait_call(170 + (i % 3)))
            seen.append((i, decision.action))
        fired = [a for _, a in seen if a != "none"]
        assert fired == ["nudge", "nudge", "force_end"], seen
        assert wd.force_ended is True

    def test_force_end_not_reached_when_nudges_break_the_loop(self):
        """Model heeds nudge 1 by doing something else → streak resets."""
        wd = RepetitionWatchdogState()
        for t in (178, 178, 178, 178):
            wd.observe(*self._wait_call(t))
        decision = wd.observe(*self._wait_call(178))
        assert decision.action == "nudge"
        # Model switches strategy: different call resets everything.
        decision = wd.observe("process", {"action": "log", "session_id": "proc_cbfe77a69afa"})
        assert decision.action == "none"
        assert decision.streak == 1
        # ...and a fresh 4-call wait streak is still under the threshold.
        for t in (178, 178, 178):
            assert wd.observe(*self._wait_call(t)).action == "none"
        assert wd.force_ended is False

    def test_nudge_text_names_the_exact_call(self):
        wd = RepetitionWatchdogState()
        for _ in range(4):
            wd.observe(*self._wait_call())
        decision = wd.observe(*self._wait_call())
        assert decision.action == "nudge"
        assert "process" in (decision.nudge_text or "")
        assert "proc_cbfe77a69afa" in (decision.nudge_text or "")
        assert "kill" in (decision.nudge_text or "").lower()

    def test_force_end_decision_carries_streak_and_tool(self):
        wd = RepetitionWatchdogState(WatchdogConfig(max_streak=2, max_nudges=1, nudge_grace=1))
        wd.observe(*self._wait_call())            # streak 1
        assert wd.observe(*self._wait_call()).action == "nudge"      # streak 2
        d = wd.observe(*self._wait_call())        # streak 3 → force end (2 + 1*1)
        assert d.action == "force_end"
        assert d.tool_name == "process"
        assert d.streak == 3


# ---------------------------------------------------------------------------
# 3. No false positives on legitimate patterns
# ---------------------------------------------------------------------------
class TestLegitimateRepeats:
    def test_poll_log_alternation_never_accumulates(self):
        wd = RepetitionWatchdogState()
        for i in range(30):
            d1 = wd.observe("process", {"action": "poll", "session_id": "s1"})
            d2 = wd.observe("process", {"action": "log", "session_id": "s1"})
            assert d1.action == "none"
            assert d2.action == "none"
        assert wd.force_ended is False

    def test_retry_after_transient_error_gets_grace(self):
        """A couple of deliberate identical retries stay under threshold."""
        wd = RepetitionWatchdogState()
        wd.observe("web_search", {"query": "hermes agent docs"})
        decision = wd.observe("web_search", {"query": "hermes agent docs"})
        assert decision.action == "none"
        # Three deliberate identical retries (streak 4) are still advisory-free.
        assert wd.observe("web_search", {"query": "hermes agent docs"}).action == "none"
        assert wd.observe("web_search", {"query": "hermes agent docs"}).action == "none"
        # The 5th consecutive identical call is the nudge point.
        assert wd.observe("web_search", {"query": "hermes agent docs"}).action == "nudge"

    def test_disabled_config_never_fires(self):
        wd = RepetitionWatchdogState(WatchdogConfig(enabled=False))
        for _ in range(50):
            decision = wd.observe("process", {"action": "wait", "session_id": "s"})
            assert decision.action == "none"
        assert wd.force_ended is False

    def test_reset_for_turn_clears_streak_and_nudges(self):
        wd = RepetitionWatchdogState()
        for _ in range(5):
            wd.observe("process", {"action": "wait", "session_id": "s"})
        wd.reset_for_turn()
        assert wd.observe("process", {"action": "wait", "session_id": "s"}).streak == 1
        assert wd.observe("process", {"action": "wait", "session_id": "s"}).action == "none"

    def test_config_section_parsing(self):
        wd = RepetitionWatchdogState.from_config_section(
            {"enabled": "false", "max_streak": 9}
        )
        assert wd.config.enabled is False
        assert wd.config.max_streak == 9
        wd2 = RepetitionWatchdogState.from_config_section("garbage")
        assert wd2.config == WatchdogConfig()


# ---------------------------------------------------------------------------
# 4. Executor seam integration — both paths observe calls
# ---------------------------------------------------------------------------
def _middleware_agent():
    """Minimal agent double for the middleware seam (no full AIAgent init)."""
    from agent.tool_call_repetition_watchdog import RepetitionWatchdogState

    agent = MagicMock()
    agent.quiet_mode = True
    agent.verbose_logging = False
    agent.tool_progress_mode = "off"
    agent._interrupt_requested = False
    agent._incremental_persistence_failed = False
    agent.tool_progress_callback = None
    agent.tool_start_callback = None
    agent.tool_complete_callback = None
    agent._checkpoint_mgr = MagicMock()
    agent._checkpoint_mgr.enabled = False
    agent._tool_guardrails = MagicMock()
    agent._tool_guardrails.before_call.return_value = SimpleNamespace(
        allows_execution=True, message=None
    )
    agent._tool_guardrails.observe_call.return_value = SimpleNamespace(notice=None, stub=None)
    agent._context_engine_tool_names = None
    agent._memory_manager = None
    agent._todo_store = None
    agent._repetition_watchdog = RepetitionWatchdogState(
        WatchdogConfig(max_streak=3, max_nudges=1, nudge_grace=1)
    )
    agent._repetition_watchdog_pending_nudge = None
    agent._repetition_watchdog_force_end = False
    return agent


def _run_one_sequential_call(agent, tool_name, arguments_json):
    """Drive ONE call through the real sequential executor (real seam)."""
    from agent.tool_executor import execute_tool_calls_sequential

    tool_call = SimpleNamespace(
        id="call_seq_1",
        type="function",
        function=SimpleNamespace(name=tool_name, arguments=arguments_json),
    )
    assistant_message = SimpleNamespace(content="", tool_calls=[tool_call])
    messages: list = []
    agent._flush_messages_to_session_db = MagicMock(return_value=True)

    with (
        patch("run_agent.handle_function_call", return_value='{"status":"timeout"}') as disp,
        patch(
            "agent.tool_executor.maybe_persist_tool_result",
            side_effect=lambda **kwargs: kwargs["content"],
        ),
        patch("agent.tool_executor._run_tool_activity_heartbeat"),
    ):
        execute_tool_calls_sequential(agent, assistant_message, messages, "task-1")

    assert disp.call_count == 1
    return messages


def _run_one_concurrent_call(agent, tool_name, arguments_json):
    """Drive ONE call through the real concurrent executor (real seam)."""
    from agent.tool_executor import execute_tool_calls_concurrent

    tool_call = SimpleNamespace(
        id="call_conc_1",
        type="function",
        function=SimpleNamespace(name=tool_name, arguments=arguments_json),
    )
    assistant_message = SimpleNamespace(content="", tool_calls=[tool_call])
    messages: list = []
    agent._flush_messages_to_session_db = MagicMock(return_value=True)

    with (
        patch.object(agent, "_invoke_tool", return_value='{"status":"timeout"}') as disp,
        patch(
            "agent.tool_executor.maybe_persist_tool_result",
            side_effect=lambda **kwargs: kwargs["content"],
        ),
    ):
        execute_tool_calls_concurrent(agent, assistant_message, messages, "task-1")

    assert disp.call_count == 1
    return messages


class TestExecutorSeamObservation:
    def test_sequential_path_accumulates_streak_and_nudges(self):
        agent = _middleware_agent()
        args = '{"action": "wait", "session_id": "proc_x", "timeout": 178}'
        # max_streak=3 in this double: 2 calls silent, 3rd fires the nudge.
        _run_one_sequential_call(agent, "process", args)
        assert agent._repetition_watchdog._streak == 1
        _run_one_sequential_call(agent, "process", args)
        assert agent._repetition_watchdog._streak == 2
        msgs = _run_one_sequential_call(agent, "process", args)
        assert agent._repetition_watchdog._streak == 3
        # The nudge lands as a synthetic user message after the tool result.
        synthetic = [m for m in msgs if m.get("role") == "user"]
        assert synthetic, "no synthetic nudge message appended"
        assert synthetic[0].get("_repetition_watchdog_synthetic") is True
        assert "repetition watchdog" in synthetic[0]["content"].lower()

    def test_sequential_timeout_drift_still_accumulates(self):
        agent = _middleware_agent()
        _run_one_sequential_call(agent, "process", '{"action": "wait", "session_id": "proc_x", "timeout": 170}')
        _run_one_sequential_call(agent, "process", '{"action": "wait", "session_id": "proc_x", "timeout": 175}')
        assert agent._repetition_watchdog._streak == 2, "timeout drift broke the streak"

    def test_concurrent_path_accumulates_streak(self):
        agent = _middleware_agent()
        args = '{"action": "wait", "session_id": "proc_y", "timeout": 178}'
        _run_one_concurrent_call(agent, "process", args)
        _run_one_concurrent_call(agent, "process", args)
        assert agent._repetition_watchdog._streak == 2
        msgs = _run_one_concurrent_call(agent, "process", args)
        assert agent._repetition_watchdog._streak == 3
        synthetic = [m for m in msgs if m.get("role") == "user"]
        assert synthetic, "no synthetic nudge message appended on concurrent path"

    def test_force_end_flag_set_when_streak_persists_past_budget(self):
        agent = _middleware_agent()
        # max_streak=3, max_nudges=1, grace=1 → force_end at streak 5.
        args = '{"action": "wait", "session_id": "proc_z", "timeout": 178}'
        for _ in range(3):
            _run_one_sequential_call(agent, "process", args)
        _run_one_sequential_call(agent, "process", args)  # 4: grace
        _run_one_sequential_call(agent, "process", args)  # 5: force-end
        assert agent._repetition_watchdog.force_ended is True
        assert agent._repetition_watchdog_force_end is True

    def test_different_call_resets_seam_streak(self):
        agent = _middleware_agent()
        args = '{"action": "wait", "session_id": "proc_x", "timeout": 178}'
        _run_one_sequential_call(agent, "process", args)
        _run_one_sequential_call(agent, "process", args)
        _run_one_sequential_call(
            agent, "process", '{"action": "log", "session_id": "proc_x"}'
        )
        assert agent._repetition_watchdog._streak == 1


# ---------------------------------------------------------------------------
# 5. Loop-level E2E: run_conversation force-ends a wedged turn (run-244)
# ---------------------------------------------------------------------------
class TestLoopForceEnd:
    def test_run_conversation_force_ends_wedged_wait_loop(self):
        """The run-244 scenario end-to-end: a model that only re-issues the
        same process(wait) gets nudged, then the turn is force-ended with a
        clear finish reason instead of looping to max_iterations."""
        from tests.run_agent.test_tool_call_incremental_persistence import (
            _make_agent,
            _mock_response,
            _mock_tool_call,
        )
        from agent.tool_call_repetition_watchdog import (
            REPETITION_WATCHDOG_FINISH_REASON,
            WatchdogConfig,
        )

        agent = _make_agent()
        # Tight config so the E2E is fast: nudge at 3, force-end at 4.
        agent._repetition_watchdog = RepetitionWatchdogState(
            WatchdogConfig(max_streak=3, max_nudges=1, nudge_grace=1)
        )
        agent._repetition_watchdog_pending_nudge = None
        agent._repetition_watchdog_force_end = False

        # web_search is in the test double's registered tool defs; a wedged
        # model re-issues the identical call every turn (timeout numeric
        # varies, which the signature normalization must ignore).
        def _wedge_turn(call_id, timeout):
            return _mock_response(
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    _mock_tool_call(
                        name="web_search",
                        arguments=f'{{"query": "same query", "timeout": {timeout}}}',
                        call_id=call_id,
                    )
                ],
            )

        # Turn sequence: wedge(1), wedge(2), wedge(3 → nudge), wedge(4 → force-end)
        agent.client.chat.completions.create.side_effect = [
            _wedge_turn("c1", 178),
            _wedge_turn("c2", 170),   # timeout drift must NOT reset the streak
            _wedge_turn("c3", 175),
            _wedge_turn("c4", 178),
        ]

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.handle_function_call", return_value='{"results": []}'),
        ):
            result = agent.run_conversation("search it")

        assert result["turn_exit_reason"] == REPETITION_WATCHDOG_FINISH_REASON
        assert result["completed"] is True
        # The wedge never reached max_iterations: exactly 4 model turns.
        assert agent.client.chat.completions.create.call_count == 4
        assert "repetition watchdog" in result["final_response"].lower()
        # Flag consumed so a resumed turn starts clean.
        assert agent._repetition_watchdog_force_end is False


# ---------------------------------------------------------------------------
# Nudge message shape (consumed by the conversation loop)
# ---------------------------------------------------------------------------
class TestNudgeMessage:
    def test_nudge_is_bounded_and_actionable(self):
        text = build_repetition_nudge(
            tool_name="process",
            args_preview='{"action":"wait","session_id":"s"}',
            streak=7,
            nudge_number=2,
            max_nudges=2,
        )
        assert len(text) < 1600
        assert text.startswith("[System:")
        assert "process" in text
        assert "ended automatically" in text
