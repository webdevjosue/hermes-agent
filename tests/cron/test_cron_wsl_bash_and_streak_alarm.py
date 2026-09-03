"""Regression tests for fleet bug t_d5cb7ff1.

Covers two defects:

1. WSL-bash path mangling: ``_run_job_script`` resolved ``bash`` with a bare
   ``shutil.which("bash")``, which on Windows can return the WSL launcher
   (``C:\\Windows\\System32\\bash.exe``) for gateway/CLI-started processes
   whose PATH lacks Git Bash. The WSL launcher strips unquoted backslashes as
   shell escapes and cannot read NTFS paths, so a Windows abs script path
   became ``C:UsersJosue...wake.sh`` and every fire died with exit 127
   (observed: cron job 661b4905fe9b, failure_streak=77, silent for 30h).

2. Invisible failure streaks: a no_agent job failing every run surfaced only
   in per-run output files and jobs.json — for ``deliver=local`` jobs nothing
   ever left the process. The fix escalates at streak >= threshold with an
   ERROR log + one-shot home-channel ping (alert-once per streak), and
   ``hermes cron doctor`` now reports long streaks.
"""

from __future__ import annotations

import os
import pathlib

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME per test (same shape as test_cron_no_agent)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib

    import cron.scheduler
    importlib.reload(cron.scheduler)
    import cron.jobs
    importlib.reload(cron.jobs)

    return home


# ---------------------------------------------------------------------------
# 1. Bash resolution: a WSL which() hit must never run the script
# ---------------------------------------------------------------------------


def _wsl_bash_path() -> str | None:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    cand = os.path.join(root, "System32", "bash.exe")
    return cand if os.path.isfile(cand) else None


def test_run_job_script_ignores_wsl_bash_from_which(hermes_env, monkeypatch):
    """Production condition: which('bash') -> WSL launcher. The script must
    still run — _find_cron_bash must prefer a Git-for-Windows bash over the
    System32 WSL launcher, and the backslash abs path must survive verbatim.

    On machines without WSL bash this monkeypatch is the exact stand-in for
    the reported condition; on machines WITH it, the probe path is real."""
    import cron.scheduler as sched

    wsl = _wsl_bash_path() or r"C:\Windows\System32\bash.exe"

    script = hermes_env / "scripts" / "probe.sh"
    script.write_text("#!/bin/bash\necho BACKSLASH_PATH_OK\n", encoding="utf-8")

    real_which = sched.shutil.which
    monkeypatch.setattr(
        sched.shutil, "which",
        lambda name, **kw: (wsl if name == "bash" else real_which(name, **kw)),
    )
    monkeypatch.delenv("HERMES_GIT_BASH_PATH", raising=False)

    ok, output = sched._run_job_script("probe.sh")
    assert ok is True, output
    assert "BACKSLASH_PATH_OK" in output


def test_find_cron_bash_never_returns_system32_wsl(monkeypatch):
    """The resolver itself: a System32 which() hit must be discarded on
    Windows regardless of candidates — WSL bash can never execute the NTFS
    path arguments we pass."""
    import cron.scheduler as sched

    if os.name != "nt":
        pytest.skip("Windows-only bash resolution")

    real_which = sched.shutil.which
    monkeypatch.setattr(
        sched.shutil, "which",
        lambda name, **kw: r"C:\Windows\System32\bash.exe",
    )
    monkeypatch.delenv("HERMES_GIT_BASH_PATH", raising=False)

    got = sched._find_cron_bash()
    if got is None:
        pytest.skip("no Git-for-Windows bash on this machine")
    assert os.path.normcase(got) != os.path.normcase(r"C:\Windows\System32\bash.exe")
    assert "System32" not in got


def test_find_cron_bash_prefers_env_override(monkeypatch):
    """HERMES_GIT_BASH_PATH wins when it points at a real file."""
    import cron.scheduler as sched

    if os.name != "nt":
        pytest.skip("Windows-only bash resolution")

    fake = pathlib.Path(sched._get_hermes_home()) / "fake_bash.exe"
    fake.write_bytes(b"not a real bash, just a marker file")
    monkeypatch.setenv("HERMES_GIT_BASH_PATH", str(fake))
    assert sched._find_cron_bash() == str(fake)


def test_find_cron_bash_posix_passthrough(monkeypatch):
    """On POSIX the resolver keeps the legacy resolution contract."""
    import cron.scheduler as sched

    monkeypatch.setattr(sched.os, "name", "posix")
    monkeypatch.setattr(
        sched.shutil, "which", lambda name, **kw: "/usr/bin/bash"
    )
    assert sched._find_cron_bash() == "/usr/bin/bash"


# ---------------------------------------------------------------------------
# 2. Failure-streak alarm
# ---------------------------------------------------------------------------


def _failing_job(hermes_env, streak: int) -> dict:
    (hermes_env / "scripts" / "broken.sh").write_text(
        "#!/bin/bash\nexit 7\n", encoding="utf-8"
    )
    return {
        "id": "j-alarm",
        "name": "broken-watchdog",
        "no_agent": True,
        "script": "broken.sh",
        "failure_streak": streak,
        "deliver": "local",
        "schedule": {"kind": "interval"},
    }


def test_failure_streak_alarm_level_thresholds(hermes_env, monkeypatch):
    from cron.scheduler import _no_agent_failure_streak_alarm_level

    monkeypatch.setattr(
        "cron.scheduler.load_config",
        lambda: {"cron": {"failure_streak_alarm_threshold": 10}},
    )
    escalate = lambda streak: _no_agent_failure_streak_alarm_level(
        {"no_agent": True, "failure_streak": streak, "schedule": {"kind": "interval"}}
    )
    assert escalate(10) == "escalate"
    assert escalate(77) == "escalate"
    assert escalate(9) == "quiet"
    # agent jobs and one-shots never escalate
    assert _no_agent_failure_streak_alarm_level(
        {"no_agent": False, "failure_streak": 99, "schedule": {"kind": "interval"}}
    ) == "quiet"
    assert _no_agent_failure_streak_alarm_level(
        {"no_agent": True, "failure_streak": 99, "schedule": {"kind": "once"}}
    ) == "quiet"


def test_failure_streak_alarm_can_be_disabled(hermes_env, monkeypatch):
    from cron.scheduler import _no_agent_failure_streak_alarm_level

    monkeypatch.setattr(
        "cron.scheduler.load_config",
        lambda: {"cron": {"failure_streak_alarm_threshold": 0}},
    )
    assert _no_agent_failure_streak_alarm_level(
        {"no_agent": True, "failure_streak": 99, "schedule": {"kind": "interval"}}
    ) == "quiet"


def test_no_agent_failure_escalates_to_home_channels(hermes_env, monkeypatch):
    """End-to-end: a deliver=local no_agent job at streak 12 fails again —
    an escalation ping must leave through the FAILURE lane routed at all
    home channels, exactly once per streak."""
    import cron.scheduler as sched

    job = _failing_job(hermes_env, streak=12)

    delivered = []
    monkeypatch.setattr(
        sched, "_deliver_result",
        lambda j, content, **kw: delivered.append(
            {"deliver": j.get("deliver"), "fd": j.get("failure_deliver"),
             "content": content, "for_failure": kw.get("for_failure")}
        ),
    )
    alarmed = []
    monkeypatch.setattr(
        sched, "mark_streak_alarm_alerted",
        lambda jid: alarmed.append(jid) or False,
    )

    success, doc, alert, error = sched.run_job(job)

    assert success is False
    assert error and "code 7" in error
    assert alarmed == ["j-alarm"]
    assert len(delivered) == 1, delivered
    d = delivered[0]
    assert d["deliver"] == "all" and d["fd"] == "all"
    assert d["for_failure"] is True
    assert "broken-watchdog" in d["content"]
    assert "12 runs in a row" in d["content"]


def test_no_agent_failure_alarm_is_alert_once_per_streak(hermes_env, monkeypatch):
    """Once the marker reports already-alerted, the ping must not re-fire."""
    import cron.scheduler as sched

    job = _failing_job(hermes_env, streak=12)
    delivered = []
    monkeypatch.setattr(
        sched, "_deliver_result",
        lambda j, content, **kw: delivered.append(content),
    )
    monkeypatch.setattr(sched, "mark_streak_alarm_alerted", lambda jid: True)

    sched.run_job(job)
    assert delivered == []


def test_no_agent_failure_below_threshold_stays_local(hermes_env, monkeypatch):
    """Streak below threshold: no escalation ping, failure alert intact."""
    import cron.scheduler as sched

    job = _failing_job(hermes_env, streak=2)
    delivered = []
    monkeypatch.setattr(
        sched, "_deliver_result",
        lambda j, content, **kw: delivered.append(content),
    )
    monkeypatch.setattr(sched, "mark_streak_alarm_alerted", lambda jid: False)

    success, doc, alert, error = sched.run_job(job)

    assert success is False
    assert delivered == []
    assert "script failed" in alert.lower()


def test_streak_alarm_marker_roundtrip(hermes_env):
    """jobs.py marker: set->True-once, clear on successful mark_job_run."""
    from cron.jobs import (
        clear_streak_alarm_alerted,
        create_job,
        get_job,
        mark_job_run,
        mark_streak_alarm_alerted,
    )

    job = create_job(prompt="p", schedule="every 5m", deliver="local")
    jid = job["id"]
    assert mark_streak_alarm_alerted(jid) is False  # first alert
    assert mark_streak_alarm_alerted(jid) is True   # already alerted
    assert get_job(jid).get("streak_alarm_alerted") is True
    # a successful run clears the marker so a NEW streak may re-alert
    mark_job_run(jid, True, None)
    assert "streak_alarm_alerted" not in (get_job(jid) or {})
    assert mark_streak_alarm_alerted(jid) is False  # re-armed


def test_doctor_reports_long_failure_streak():
    """`hermes cron doctor` surfaces streak >= 10 as an issue."""
    from hermes_cli.cron import _cron_doctor_issues_for_job, _STREAK_ALARM_THRESHOLD

    hot = {"failure_streak": 77, "enabled": True, "state": "scheduled",
           "next_run_at": "2999-01-01T00:00:00+00:00"}
    issues = _cron_doctor_issues_for_job(hot)
    assert any("failure_streak=77" in i for i in issues)

    cold = {"failure_streak": 3, "enabled": True, "state": "scheduled",
            "next_run_at": "2999-01-01T00:00:00+00:00"}
    assert not any("failure_streak" in i for i in _cron_doctor_issues_for_job(cold))
    assert _STREAK_ALARM_THRESHOLD == 10
