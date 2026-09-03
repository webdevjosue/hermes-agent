"""Regression tests: `hermes gateway install` must never destroy the existing
Scheduled Task when schtasks is unavailable/broken (Task Scheduler service
down, RPC unreachable, corrupted scheduler state).

Observed in the wild (2026-09-01..03, DESKTOP-EFLEHMQ): the Windows Task
Scheduler service died at boot (sc query schedule -> STOPPED; every schtasks
call returns "ERROR: The network address is invalid"). Gateway install ran
``/Delete /F`` FIRST and only then attempted ``/Create /F`` — when create
failed, the previously working task had already been deleted. Net effect:
install DEGRADED autostart from "working task" to "nothing", then raised.

Contract pinned here:

* ``_install_scheduled_task`` must probe with ``/Create /F`` (which
  force-replaces) WITHOUT a preceding /Delete when that delete is not
  guaranteed to be needed — if create fails, the existing task survives.
* The old task must survive a failed create: delete only happens when the
  create succeeded (or never at all, since /Create /F replaces in place).
* install() must fall back to the Startup-folder entry instead of raising
  when schtasks fails with a scheduler-level error (service down), because
  the Startup folder is an independent autostart mechanism that still works.
"""

from __future__ import annotations

import pytest

from hermes_cli import gateway_windows


class _FakeSchtasks:
    """Scriptable schtasks stand-in that records every argv it sees."""

    def __init__(self, *, task_exists: bool = True):
        self.task_exists = task_exists
        self.calls: list[list[str]] = []
        self.scheduler_down = True  # every operation fails with RPC error

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        op = args[0] if args else ""
        if self.scheduler_down:
            return (1, "", "ERROR: The network address is invalid.")
        if op == "/Delete":
            self.task_exists = False
            return (0, "", "")
        if op == "/Create":
            self.task_exists = True
            return (0, "SUCCESS: The scheduled task has been created.", "")
        if op == "/Query":
            return ((0, "TaskName Hermes_Gateway_test", "") if self.task_exists
                    else (1, "", "ERROR: The task does not exist."))
        return (1, "", "ERROR: unknown op")


@pytest.fixture()
def fake_schtasks(monkeypatch):
    fake = _FakeSchtasks()
    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake)
    return fake


@pytest.fixture()
def quiet_install_env(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: True)
    monkeypatch.setattr(gateway_windows, "_write_task_script", lambda: tmp_path / "gw.cmd")
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_test")


class TestInstallKeepsExistingTaskWhenSchedulerIsDown:
    def test_failed_create_leaves_task_undeleted(self, fake_schtasks, quiet_install_env, tmp_path):
        """Scheduler down: create fails AND no /Delete was ever issued."""
        fake_schtasks.task_exists = True
        fake_schtasks.scheduler_down = True

        ok, detail = gateway_windows._install_scheduled_task(
            "Hermes_Gateway_test", tmp_path / "gw.cmd"
        )
        assert ok is False
        assert "network address is invalid" in detail.lower()

    def test_no_delete_when_scheduler_unreachable(self, fake_schtasks, quiet_install_env, tmp_path):
        fake_schtasks.scheduler_down = True
        ok, detail = gateway_windows._install_scheduled_task(
            "Hermes_Gateway_test", tmp_path / "gw.cmd"
        )
        assert ok is False
        deletes = [c for c in fake_schtasks.calls if c and c[0] == "/Delete"]
        assert deletes == [], (
            "install must not /Delete when the scheduler is unreachable — "
            "/Create /F alone force-replaces an existing task"
        )

    def test_successful_create_replaces_without_delete(self, fake_schtasks, quiet_install_env, tmp_path):
        """Healthy scheduler: create succeeds; the pre-delete is gone entirely
        (it was only ever there to clear stale settings; /Create /F overwrites
        the whole task definition)."""
        fake_schtasks.task_exists = True
        fake_schtasks.scheduler_down = False

        ok, detail = gateway_windows._install_scheduled_task(
            "Hermes_Gateway_test", tmp_path / "gw.cmd"
        )

        assert ok is True
        deletes = [c for c in fake_schtasks.calls if c and c[0] == "/Delete"]
        assert deletes == [], "healthy path must rely on /Create /F replace-in-place"
        creates = [c for c in fake_schtasks.calls if c and c[0] == "/Create"]
        assert creates, "create must still be attempted"


class TestSchedulerDownFallsBackToStartupFolder:
    def test_install_uses_startup_fallback_instead_of_raising(
        self, fake_schtasks, quiet_install_env, monkeypatch, tmp_path
    ):
        """install() end-to-end: scheduler down -> Startup entry installed,
        no RuntimeError raised."""
        fake_schtasks.task_exists = True
        fake_schtasks.scheduler_down = True

        startup_entries: list = []
        monkeypatch.setattr(
            gateway_windows,
            "_install_startup_entry",
            lambda script_path: startup_entries.append(script_path) or tmp_path / "startup.vbs",
        )
        monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda sn, sol: (True, True))
        monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda: 4242)
        monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
        monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda src: None)
        monkeypatch.setattr(gateway_windows, "_print_next_steps", lambda: None)
        from hermes_cli import setup as setup_mod
        monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **k: False)

        # Must not raise even though schtasks is completely broken.
        gateway_windows.install(start_now=True, start_on_login=True)

        assert startup_entries, (
            "Startup-folder entry must be installed when the Task Scheduler is down"
        )


class TestFallbackPatternCoversSchedulerDown:
    def test_network_address_invalid_is_fallback(self):
        detail = "schtasks /Create failed (code 1): ERROR: The network address is invalid."
        assert gateway_windows._should_fall_back(1, detail) is True

    def test_random_schtasks_error_is_not_fallback(self):
        assert gateway_windows._should_fall_back(1, "ERROR: something else entirely") is False
