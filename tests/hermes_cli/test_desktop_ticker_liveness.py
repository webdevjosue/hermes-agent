"""D-A regression: liveness surfaces must see the desktop all-profiles ticker.

Live incident (2026-09-04, t_f93c28f1): a profile-scoped CLI
(``HERMES_HOME=profiles/sentinel hermes gateway status`` / ``cron add`` /
``cron list``) reported the gateway as DOWN while sentinel's cron jobs were
verifiably firing on schedule (03:45, 04:00 — executions.db rows completed
by the desktop ``serve`` backend, pid 8300).

Root cause: every liveness surface equates "the builtin cron ticker" with
"this profile's own gateway.pid / runtime lock" — plus the multiplexer
escape hatch ``named_profile_served_by_running_multiplexer`` which requires
``gateway.multiplex_profiles: true`` in the DEFAULT config. This fleet runs
multiplex OFF: the default home's ticker that fires secondary jobs is the
DESKTOP APP's backend, whose ticker is started with
``profiles_to_serve(multiplex=True)`` unconditionally
(hermes_cli/web_server.py) — a topology none of the probes model.

Contract pinned here: when the current profile has no own gateway AND no
multiplexer serves it, liveness must additionally recognize a live desktop
``serve`` backend (``hermes_cli.main --profile <name> serve`` /
``--profile default serve`` with the all-profiles desktop ticker) as a
dispatch trigger for this profile's cron store — reporting it as running,
naming the backend, never as a bare "not running" false negative.

The detector must be cheap and side-effect free: it is called from
interactive CLI surfaces (``gateway status``, ``cron list``/``add``) and
``cron status``.
"""

from __future__ import annotations

import sys
from unittest.mock import patch


def _no_own_gw(monkeypatch):
    """Neutralize own-gateway + multiplexer probes."""
    monkeypatch.setattr(
        "gateway.status.is_gateway_runtime_lock_active",
        lambda lock_path=None: False,
    )
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda *a, **k: [])
    monkeypatch.setattr(
        "hermes_cli.gateway.named_profile_served_by_running_multiplexer",
        lambda *a, **k: False,
    )


class TestDetectDesktopCronTicker:
    def test_default_serve_backend_counts(self, monkeypatch, tmp_path):
        from hermes_cli import gateway as gw

        _no_own_gw(monkeypatch)
        fake = [
            (
                4242,
                "python -m hermes_cli.main --profile default serve --host 127.0.0.1 --port 0",
            ),
            (5555, "python -m hermes_cli.main gateway status"),
        ]
        with patch.object(gw, "_iter_serve_backend_command_lines", return_value=iter(fake)):
            hit = gw.detect_desktop_cron_ticker_for_profile("sentinel")
        assert hit is not None and hit[0] == 4242
        assert "serve" in hit[1]

    def test_named_serve_backend_counts_for_its_own_profile(self, monkeypatch, tmp_path):
        from hermes_cli import gateway as gw

        _no_own_gw(monkeypatch)
        fake = [
            (
                4243,
                "python -m hermes_cli.main --profile sentinel serve --host 127.0.0.1 --port 0",
            ),
        ]
        with patch.object(gw, "_iter_serve_backend_command_lines", return_value=iter(fake)):
            hit = gw.detect_desktop_cron_ticker_for_profile("sentinel")
        assert hit is not None and hit[0] == 4243

    def test_other_named_profile_backend_does_not_count(self, monkeypatch, tmp_path):
        from hermes_cli import gateway as gw

        _no_own_gw(monkeypatch)
        fake = [
            (
                4244,
                "python -m hermes_cli.main --profile backend serve --host 127.0.0.1 --port 0",
            ),
        ]
        with patch.object(gw, "_iter_serve_backend_command_lines", return_value=iter(fake)):
            assert gw.detect_desktop_cron_ticker_for_profile("sentinel") is None

    def test_gateway_run_processes_do_not_count(self, monkeypatch, tmp_path):
        from hermes_cli import gateway as gw

        _no_own_gw(monkeypatch)
        fake = [
            (19544, "python -m hermes_cli.main gateway run"),
        ]
        with patch.object(gw, "_iter_serve_backend_command_lines", return_value=iter(fake)):
            assert gw.detect_desktop_cron_ticker_for_profile("sentinel") is None


class TestBuiltinGatewayLivenessSeesDesktopTicker:
    def test_named_profile_with_desktop_default_backend_reports_true(self, monkeypatch, tmp_path):
        import hermes_cli.cron as cron_cli
        from hermes_constants import get_default_hermes_root

        _no_own_gw(monkeypatch)
        fake = [
            (
                8300,
                "python -m hermes_cli.main --profile default serve --host 127.0.0.1 --port 0",
            ),
        ]
        from hermes_cli import gateway as gw

        with patch.object(gw, "_iter_serve_backend_command_lines", return_value=iter(fake)):
            assert cron_cli._builtin_gateway_liveness() is True

    def test_no_backend_stays_false(self, monkeypatch, tmp_path):
        import hermes_cli.cron as cron_cli

        _no_own_gw(monkeypatch)
        from hermes_cli import gateway as gw

        with patch.object(gw, "_iter_serve_backend_command_lines", return_value=iter([])):
            assert cron_cli._builtin_gateway_liveness() is False

    def test_default_profile_also_counts_desktop_ticker(self, monkeypatch, tmp_path):
        """The desktop default-backend ticks the DEFAULT store too
        (profiles_to_serve(multiplex=True) includes default; its profile_gate
        stands down only when the profile's own gateway runs — which the
        lock/pid probes detect first). With the default gateway process
        down but the desktop app open, default-home jobs still fire."""
        import hermes_cli.cron as cron_cli

        _no_own_gw(monkeypatch)
        fake = [
            (
                8300,
                "python -m hermes_cli.main --profile default serve --host 127.0.0.1 --port 0",
            ),
        ]
        from hermes_cli import gateway as gw

        with patch.object(gw, "_iter_serve_backend_command_lines", return_value=iter(fake)):
            assert cron_cli._builtin_gateway_liveness() is True


class TestGatewayWindowsStatusMentionsDesktopTicker:
    def test_status_names_the_desktop_backend_when_no_profile_gateway(self, monkeypatch, tmp_path, capsys):
        from hermes_cli import gateway_windows

        monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: False)
        monkeypatch.setattr(gateway_windows, "is_startup_entry_installed", lambda: True)
        monkeypatch.setattr(gateway_windows, "get_startup_entry_path", lambda: tmp_path / "s.vbs")
        monkeypatch.setattr(gateway_windows, "_legacy_startup_entry_path", lambda: tmp_path / "s.cmd")
        monkeypatch.setattr(gateway_windows, "_gateway_pids", lambda: [])
        from hermes_cli import gateway as gw

        fake = [
            (
                8300,
                "python -m hermes_cli.main --profile default serve --host 127.0.0.1 --port 0",
            ),
        ]
        with patch.object(gw, "_iter_serve_backend_command_lines", return_value=iter(fake)):
            gateway_windows.status()
        out = capsys.readouterr().out
        assert "No gateway process detected" not in out
        assert "desktop app backend (PID 8300)" in out
        assert "ticks this profile's cron" in out
