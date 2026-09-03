"""Regression guard: Z.AI Coding transient 401/1000 must not kill aux tasks.

Fleet incident 2026-08-31/09-01 (card t_832d0510): the Coding Plan endpoint
(https://api.z.ai/api/coding/paas/v4) intermittently returns HTTP 401 body
code 1000 "Authentication Failed" / "身份验证失败。" under concurrent load,
while the SAME key succeeds seconds later in the same session (frontend
agent.log 23:04:42 fail -> 23:05:46 success -> 23:11:25 fail). The main-loop
retry (t_340a8a3f, conversation_loop.py) covers main chat calls only. The
auxiliary client (vision_analyze's call path via call_llm(task="vision"))
classifies 401 as auth (not capacity), and with an EXPLICIT provider
(auxiliary.vision.provider: zai — the fleet-wide config) the fallback gate
never opens, so the error surfaces to the tool in <1s and visual QC dies.

Fix under test (this file's RED): a bounded same-provider retry for the
scoped transient shape (is_zai_coding_transient_auth_error) in BOTH
call_llm sync and async_call_llm, BEFORE the auth-refresh / pool /
fallback rungs. A genuinely dead key exhausts the budget and falls through
to the unchanged handling (pool rotation → fallback → raise).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ZAI_CODING_BASE = "https://api.z.ai/api/coding/paas/v4"
ZAI_TRANSIENT_AUTH_MESSAGE = (
    "Error code: 401 - {'error': {'code': '1000', 'message': 'Authentication Failed'}}"
)


class _ZaiTransientAuthError(Exception):
    """Stand-in for openai.AuthenticationError with the incident shape."""

    status_code = 401

    def __init__(self, message: str = ZAI_TRANSIENT_AUTH_MESSAGE):
        super().__init__(message)
        self.message = message
        self.body = {"error": {"code": "1000", "message": "Authentication Failed"}}
        self.response = None


def _mock_response(content: str = "ok"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="glm-5.3-flash", usage=None)


def _vision_messages() -> list:
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }]


def _resolve_explicit_zai(monkeypatch, response_sequence):
    """Patch the vision provider resolution to an explicit zai client whose
    relay raises the transient 401 N times then succeeds."""
    import agent.auxiliary_client as aux

    calls = {"n": 0}

    client = MagicMock()
    client.base_url = ZAI_CODING_BASE
    client.api_key = "zai-key-abcdef12"

    def fake_relay(c, kwargs, **_):
        i = calls["n"]
        calls["n"] += 1
        if i < len(response_sequence):
            item = response_sequence[i]
            if isinstance(item, Exception):
                raise item
            return item
        return response_sequence[-1]

    monkeypatch.setattr(
        aux, "resolve_vision_provider_client",
        lambda **_: ("zai", client, "glm-5.3-flash"),
    )
    # call_llm's provider resolution for an explicit aux provider lands in
    # _resolve_task_provider_model; return (provider, model, base, key, mode)
    monkeypatch.setattr(
        aux, "_resolve_task_provider_model",
        lambda *a, **k: ("zai", "glm-5.3-flash", ZAI_CODING_BASE, "zai-key-abcdef12", "chat_completions"),
    )
    monkeypatch.setattr(aux, "_relay_sync_completion", fake_relay)

    async def fake_relay_async(c, kwargs, **_):
        return fake_relay(c, kwargs, **_)
    monkeypatch.setattr(aux, "_relay_async_completion", fake_relay_async)
    return calls


def _no_wait(monkeypatch):
    import agent.auxiliary_client as aux
    monkeypatch.setattr(aux, "_zai_transient_auth_backoff", lambda n: 0.0)


class TestZaiTransientAuthAuxRetry:
    def test_sync_vision_transient_401_retries_then_succeeds(self, monkeypatch):
        import agent.auxiliary_client as aux

        _resolve_explicit_zai(
            monkeypatch,
            [_ZaiTransientAuthError(), _ZaiTransientAuthError(), _mock_response("vision ok")],
        )
        # No waits in tests: patch the backoff used by the aux retry.
        _no_wait(monkeypatch)

        result = aux.call_llm(task="vision", messages=_vision_messages())
        assert "vision ok" in str(getattr(result, "content", result) if not isinstance(result, str) else result) or result

    def test_async_vision_transient_401_retries_then_succeeds(self, monkeypatch):
        import agent.auxiliary_client as aux

        _resolve_explicit_zai(
            monkeypatch,
            [_ZaiTransientAuthError(), _ZaiTransientAuthError(), _mock_response("vision ok")],
        )
        _no_wait(monkeypatch)

        result = asyncio.run(aux.async_call_llm(task="vision", messages=_vision_messages()))
        assert result is not None

    def test_sync_dead_key_exhausts_budget_and_raises(self, monkeypatch):
        """A genuinely dead key must NOT retry forever — after the bounded
        budget it falls through to the existing chain (which raises)."""
        import agent.auxiliary_client as aux

        calls = _resolve_explicit_zai(
            monkeypatch,
            [_ZaiTransientAuthError()] * 10,
        )
        _no_wait(monkeypatch)

        with pytest_raises():
            aux.call_llm(task="vision", messages=_vision_messages())
        # 1 initial + exactly 2 bounded retries = 3 attempts, not 10
        assert calls["n"] == 3, f"expected 3 attempts, got {calls['n']}"


def pytest_raises():
    import pytest
    return pytest.raises(Exception)
