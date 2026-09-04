"""D-B regression: the stale-yield gate must probe the home being TICKED.

Live incident (2026-09-04, t_f93c28f1): the desktop app's ``serve`` backend
runs a multiplex-style all-profiles cron ticker (web_server.py ticks every
profile home). The box's checkout moved (hot update) while the backend kept
running, so the backend is code-stale. A fresh ``gateway run`` then started
for the DEFAULT home only (multiplex_profiles off).

Inside the per-profile tick loop, ``set_hermes_home_override`` scopes the
store to each secondary profile (sentinel). But the stale-yield gate
``_should_yield_tick_to_fresh_gateway`` probes the gateway runtime lock via
``_get_process_hermes_home()`` — the PROCESS env var, which deliberately
ignores the contextvar override (#56986). So while ticking the sentinel
store the gate consulted the DEFAULT home's lock, saw a fresh foreign
gateway holding it, and raised CronTickYielded.

The yield contract ("fresh code picks the job up within one tick interval")
was false for sentinel's store: the fresh gateway does not multiplex, so NO
process was dispatching sentinel's jobs. Dead zone until the desktop app
restarts — heartbeat fresh, every tick "successfully yielded".

Contract pinned here: when a context-local home override is active, the
gate must probe THAT home's lock. A per-profile tick must yield only to a
fresh gateway that would actually tick this profile's store.
"""

from __future__ import annotations

from unittest.mock import patch

import cron.scheduler as scheduler_mod


def test_yield_gate_probes_overridden_home_lock(monkeypatch, tmp_path):
    """Context override active + that home's lock foreign-held → yield."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    probed_lock_paths: list = []

    def _fake_active(lock_path=None):
        probed_lock_paths.append(lock_path)
        return True  # foreign holder on the probed home's lock

    monkeypatch.setattr(scheduler_mod, "_detect_gateway_code_skew", lambda: ("b", "d"))
    from gateway import status as gateway_status

    monkeypatch.setattr(gateway_status, "owns_gateway_runtime_lock", lambda: False)
    monkeypatch.setattr(gateway_status, "is_gateway_runtime_lock_active", _fake_active)

    token = set_hermes_home_override(str(tmp_path / "profiles" / "satellite"))
    try:
        skew = scheduler_mod._should_yield_tick_to_fresh_gateway()
    finally:
        reset_hermes_home_override(token)

    assert skew == ("b", "d"), "foreign holder on the TICKED home's lock must yield"
    assert probed_lock_paths, "the gate must probe a lock"
    first = probed_lock_paths[0]
    assert first is not None, (
        "with a context-local home override active, the gate must pass the "
        "overridden home's explicit lock path — probing the process env home "
        "(None) consults the wrong profile's lock (live dead-zone incident)"
    )


def test_yield_gate_inactive_lock_on_overridden_home_proceeds(monkeypatch, tmp_path):
    """Context override active + that home's lock FREE → proceed.

    This is the live dead zone: default home's lock was foreign-held, but
    satellite's own lock was nonexistent/free — no fresh gateway ticks the
    satellite store, so yielding would silently kill its only ticker.
    """
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    monkeypatch.setattr(scheduler_mod, "_detect_gateway_code_skew", lambda: ("b", "d"))
    from gateway import status as gateway_status

    monkeypatch.setattr(gateway_status, "owns_gateway_runtime_lock", lambda: False)
    monkeypatch.setattr(gateway_status, "is_gateway_runtime_lock_active", lambda lock_path=None: False)

    token = set_hermes_home_override(str(tmp_path / "profiles" / "satellite"))
    try:
        assert scheduler_mod._should_yield_tick_to_fresh_gateway() is None
    finally:
        reset_hermes_home_override(token)


def test_yield_gate_no_override_uses_process_home_as_before(monkeypatch, tmp_path):
    """No context override → unchanged legacy behavior (env-var home)."""
    monkeypatch.setattr(scheduler_mod, "_detect_gateway_code_skew", lambda: ("b", "d"))
    from gateway import status as gateway_status

    monkeypatch.setattr(gateway_status, "owns_gateway_runtime_lock", lambda: False)
    monkeypatch.setattr(gateway_status, "is_gateway_runtime_lock_active", lambda lock_path=None: True)

    # No override active: gate probes the process home's lock (None → env var).
    assert scheduler_mod._should_yield_tick_to_fresh_gateway() == ("b", "d")


def test_tick_yields_only_when_ticked_home_has_fresh_gateway(monkeypatch, tmp_path):
    """End-to-end through tick(): satellite home override + satellite lock free
    → tick must PROCEED (acquire the tick lock), not raise CronTickYielded —
    even when the DEFAULT home's lock is held by a fresh foreign gateway."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    home = tmp_path / "profiles" / "satellite"
    (home / "cron").mkdir(parents=True)

    monkeypatch.setattr(scheduler_mod, "_detect_gateway_code_skew", lambda: ("b", "d"))
    from gateway import status as gateway_status

    monkeypatch.setattr(gateway_status, "owns_gateway_runtime_lock", lambda: False)

    def _active(lock_path=None):
        # Default home's lock (env var) is held; the satellite's explicit
        # path is free — mirror the live incident topology.
        return lock_path is None

    monkeypatch.setattr(gateway_status, "is_gateway_runtime_lock_active", _active)

    token = set_hermes_home_override(str(home))
    try:
        # Empty store: tick must complete and return 0, NOT yield.
        assert scheduler_mod.tick(verbose=False) == 0
    finally:
        reset_hermes_home_override(token)
