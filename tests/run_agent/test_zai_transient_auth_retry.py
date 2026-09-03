"""Regression guard for the 2026-08-31 Z.AI Coding transient 401/1000 crashes.

During the Z.AI peak (~23:00-03:00 PDT) the Coding Plan endpoint
(https://api.z.ai/api/coding/paas/v4) intermittently returned HTTP 401 body
code 1000 "Authentication Failed" under concurrent load, while single-call
key tests and sibling workers on the SAME account succeeded in the same
window. The generic 401 classification (FailoverReason.auth,
retryable=False) sent those workers straight to the client-error terminal —
before fleet fallback_providers existed, an instant exit (request dumps:
seo 20260831_233032_3ec490, seo 20260831_234540_e17d07, designer
20260831_232933_b33e0a, all reason=non_retryable_client_error).

The fix (2026-09-01, card t_340a8a3f) retries the SAME provider a bounded
2 times with ~30s/~60s adaptive waits BEFORE the auth-failover block, so
transient blips keep the session on the paid ZAI primary. A genuinely dead
key exhausts the budget and falls through to the unchanged auth-failover /
terminal path (fallback chain first if configured).

These tests drive the full ``run_conversation`` loop with the provider
exception raised from ``_interruptible_api_call`` (same idiom as
test_69078_image_corrupt_recovery.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent

ZAI_CODING_BASE = "https://api.z.ai/api/coding/paas/v4"
ZAI_TRANSIENT_AUTH_BODY = {"error": {"code": "1000", "message": "Authentication Failed"}}
ZAI_TRANSIENT_AUTH_MESSAGE = (
    "Error code: 401 - {'error': {'code': '1000', 'message': 'Authentication Failed'}}"
)


class _ZaiTransientAuthError(Exception):
    """Stand-in for openai.AuthenticationError with the crash shape."""

    status_code = 401

    def __init__(self, message: str = ZAI_TRANSIENT_AUTH_MESSAGE, body: dict | None = None):
        super().__init__(message)
        self.message = message
        self.body = body or dict(ZAI_TRANSIENT_AUTH_BODY)
        self.response = None


def _mock_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="glm-5.3", usage=None)


def _make_agent(**overrides):
    """Minimal AIAgent on the ZAI Coding endpoint, mirroring the idiom in
    tests/run_agent/test_32646_fallback_429_after_timeout.py."""
    kwargs = dict(
        api_key="primary-key-abcdef12",
        base_url=ZAI_CODING_BASE,
        provider="zai",
        model="glm-5.3",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    kwargs.update(overrides)
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(**kwargs)
        agent.client = MagicMock()
        return agent


class TestZaiTransientAuthBoundedRetry:
    def test_transient_401_retries_same_provider_then_recovers(self):
        """Positive path: 401/1000 → 30s wait → same-provider retry succeeds.
        The session must stay on the ZAI primary (no failover) and complete
        the turn. (Backoff patched to 0 — the real 30/60s waits are pinned
        by the unit tests in tests/test_retry_utils.py.)"""
        calls = []

        def fake_api_call(api_kwargs):
            calls.append(1)
            if len(calls) <= 1:
                raise _ZaiTransientAuthError()
            return _mock_response("Recovered on the ZAI primary.")

        agent = _make_agent()

        from agent import conversation_loop

        with patch.object(conversation_loop, "zai_coding_transient_auth_backoff", return_value=0.0):
            with patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call):
                with (
                    patch.object(agent, "_persist_session"),
                    patch.object(agent, "_save_trajectory"),
                    patch.object(agent, "_cleanup_task_resources"),
                    patch("run_agent.OpenAI", return_value=MagicMock()),
                ):
                    result = agent.run_conversation("hello")

        assert result["completed"] is True
        assert result["final_response"] == "Recovered on the ZAI primary."
        assert len(calls) == 2, "exactly one bounded same-provider retry expected"
        assert agent.provider == "zai"
        assert agent.model == "glm-5.3"

    def test_persistent_401_exhausts_budget_then_falls_through_unchanged(self):
        """Negative guard: when the 401/1000 never clears (dead key), the
        bounded budget (2 retries) exhausts and the loop takes the EXISTING
        path — auth-failover to the configured chain here — never an
        unbounded same-provider hammering."""
        from agent.retry_utils import ZAI_CODING_TRANSIENT_AUTH_RETRY_BUDGET

        calls = []

        def fake_api_call(api_kwargs):
            calls.append({"model": agent.model, "provider": agent.provider})
            if agent.provider == "zai":
                raise _ZaiTransientAuthError()
            return _mock_response("served by fallback")

        agent = _make_agent(
            fallback_model=[
                {
                    "provider": "openrouter",
                    "model": "z-ai/glm-5.3-flash",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": "fb-or-key-abcdef12",
                }
            ],
        )

        from agent import conversation_loop

        with patch.object(conversation_loop, "zai_coding_transient_auth_backoff", return_value=0.0):
            with patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call):
                with (
                    patch.object(agent, "_persist_session"),
                    patch.object(agent, "_save_trajectory"),
                    patch.object(agent, "_cleanup_task_resources"),
                    patch("run_agent.OpenAI", return_value=MagicMock()),
                ):
                    result = agent.run_conversation("hello")

        zai_calls = [c for c in calls if c["provider"] == "zai"]
        assert len(zai_calls) == 1 + ZAI_CODING_TRANSIENT_AUTH_RETRY_BUDGET, (
            "dead key must see exactly budget+1 same-provider attempts "
            f"({1 + ZAI_CODING_TRANSIENT_AUTH_RETRY_BUDGET}), got {len(zai_calls)}"
        )
        assert any(c["provider"] == "openrouter" for c in calls), (
            "after budget exhaustion the existing auth-failover must fire"
        )
        assert result["completed"] is True
        assert result["final_response"] == "served by fallback"

    def test_plain_openai_401_stays_fail_fast(self):
        """Scope guard: a 401 from the SAME endpoint/model but with a body
        code that is NOT 1000 (a genuinely rejected key) must NOT enter the
        bounded same-provider retry — it goes straight to the existing
        failover/terminal path. No fallback configured here, so the classic
        instant client-error terminal with exactly one provider call."""
        calls = []

        def fake_api_call(api_kwargs):
            calls.append(1)
            raise _ZaiTransientAuthError(
                message="Error code: 401 - {'error': {'code': '401', 'message': 'Invalid API key'}}",
                body={"error": {"code": "401", "message": "Invalid API key"}},
            )

        agent = _make_agent()

        with patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call):
            with (
                patch.object(agent, "_persist_session"),
                patch.object(agent, "_save_trajectory"),
                patch.object(agent, "_cleanup_task_resources"),
                patch("run_agent.OpenAI", return_value=MagicMock()),
                patch("agent.conversation_loop.time.sleep"),
            ):
                result = agent.run_conversation("hello")

        assert len(calls) == 1, "non-transient 401 must not be retried on the same provider"
        assert result.get("completed") is False
        assert result.get("failed") is True


class TestZaiTransientAuthBudgetState:
    def test_budget_counter_lives_on_turn_retry_state(self):
        """The per-run budget counter is a TurnRetryState field so it resets
        per API-call block and survives every in-loop code path."""
        from agent.turn_retry_state import TurnRetryState

        state = TurnRetryState()
        assert state.zai_transient_auth_retries == 0
        state.zai_transient_auth_retries += 1
        assert state.zai_transient_auth_retries == 1
