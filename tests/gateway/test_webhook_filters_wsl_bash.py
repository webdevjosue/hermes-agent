"""Regression tests for the webhook sibling of t_d5cb7ff1.

``run_route_script`` resolved bash with a bare ``shutil.which("bash")`` — the
same Windows WSL-launcher trap as cron's ``_run_job_script``. A .sh route
script under an NTFS path must run under a bash that can read it.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _make_route_runner():
    from gateway.platforms.webhook_filters import WebhookRouteProcessor

    return WebhookRouteProcessor(script_timeout_seconds=10)


def test_route_sh_script_runs_despite_wsl_bash_on_path(hermes_env, monkeypatch):
    """Production condition: which('bash') -> WSL launcher only. The route
    script must still execute (its stdout JSON used as payload)."""
    if os.name != "nt":
        pytest.skip("Windows-only bash resolution")

    wsl = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "bash.exe"
    )

    script = hermes_env / "scripts" / "route.sh"
    script.write_text(
        '#!/bin/bash\nprintf \'{"ok": true, "echo": "%s"}\\n\' "$1"\n',
        encoding="utf-8",
    )

    import gateway.platforms.webhook_filters as wf
    import tools.environments.local as tlocal

    real_which = wf.shutil.which
    # which() in BOTH modules resolves the WSL launcher — the trap condition
    monkeypatch.setattr(
        wf.shutil, "which",
        lambda name, **kw: (wsl if name == "bash" else real_which(name, **kw)),
    )
    real_local_which = tlocal.shutil.which
    monkeypatch.setattr(
        tlocal.shutil, "which",
        lambda name, **kw: (wsl if name == "bash" else real_local_which(name, **kw)),
    )
    monkeypatch.delenv("HERMES_GIT_BASH_PATH", raising=False)

    runner = _make_route_runner()
    proceed, payload = runner.run_route_script(
        "route.sh", {"event": "push"}
    )

    assert proceed is True
    assert payload and payload.get("ok") is True


def test_route_sh_script_missing_bash_ignores_cleanly(hermes_env, monkeypatch):
    """No resolvable bash at all -> webhook ignored with a warning, not a crash.

    The resolver is stubbed rather than env-starved because explicit-layout
    defaults (C:\\Program Files\\Git\\bin\\bash.exe) legitimately find a real
    bash on dev machines — the seam under test here is webhook_filters'
    handling of a None/raising resolver, not the resolver itself (which has
    its own coverage in the terminal suite)."""
    if os.name != "nt":
        pytest.skip("Windows-only bash resolution")

    script = hermes_env / "scripts" / "route.sh"
    script.write_text("#!/bin/bash\necho '{}'\n", encoding="utf-8")

    import tools.environments.local as tlocal

    monkeypatch.setattr(tlocal, "_find_bash", lambda: None)

    runner = _make_route_runner()
    proceed, payload = runner.run_route_script("route.sh", {"event": "push"})

    assert proceed is False
    assert payload is None

    # A resolver that RAISES (RuntimeError from _find_bash's Git-Bash-not-
    # found path) must degrade the same way — never crash the webhook.
    def _boom():
        raise RuntimeError("Git Bash not found.")

    monkeypatch.setattr(tlocal, "_find_bash", _boom)
    proceed2, payload2 = runner.run_route_script("route.sh", {"event": "push"})
    assert proceed2 is False
    assert payload2 is None
