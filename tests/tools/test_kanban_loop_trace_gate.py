"""Tests for the loop-closure trace completion gate (card t_f431ca9b).

Ports the fork gate (_missing_loop_trace, cards t_31b252aa + t_a617051b)
to the refactored live tree and extends it to the designer profile
(pilot #3) with concept-matching tolerance learned from the 2026-09-30
window audits (t_c4f595ac comment 731, t_33815288 comment 771):
underscore JSON keys (do_differently), metadata-nested trace objects
(loop_closure_trace / execution_trace), and the single-source rule
(rich metadata WITHOUT trace fields must NOT pass).

The REAL_PAYLOADS fixtures below are verbatim captures of the six audited
designer completions (kanban.db task_runs 455/495/493/505/506/520/523)
so the gate is regression-tested against the exact evidence that
confirmed it, not synthetic stand-ins.
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

# Verbatim summary of run 506 (t_3d0e8442) — trace lives ONLY in metadata.
_T3D0E8442_SUMMARY = (
    "Designer DNA critique + sitewide centering pass complete. Centering "
    "verdict: GREEN by measurement sitewide (control/tabs/heads/final CTA "
    "all dead-center; the hero's left column is the deliberate scrim+AA-gate "
    "solve, left untouched per light-touch law). Caught and intercepted two "
    "CEO-override regressions on frontend's live kit apply (the \"Book "
    "Now \u00b7\" label regression \u2014 root cause my own stale spec, corrected in "
    "BOOK_LOC_SPEC_v1.md; and the home last-barber chrome removal), delivered "
    "the measured center-polish v2 block + 5-gate verifier (mobile edge "
    "convergence 195/336/351\u2192370.5, cluster rhythm, ghost-chip "
    "hidden-attribute defeat) routed to frontend on t_4abd5c6a, @cmo review "
    "gate t_0b467088."
)
# Verbatim loop_closure_trace object of run 506 metadata (underscore keys).
_T3D0E8442_TRACE = {
    "attempted": "sitewide centering audit via live+local playwright rigs; "
                 "two CEO override interceptions on the hot kit apply; "
                 "spec-of-record correction; measured v2 polish block",
    "do_differently": "grep every staged spec/kit/recipe for override-affected "
                      "wording the moment an override lands (specs written "
                      "pre-override are regression vectors); name the "
                      "equivalence tier before claiming file equivalence; "
                      "never let vision dimension claims into a ruling "
                      "unmeasured",
    "failed": "3 iterations on my own gate's nested-CSS selector scanner "
              "(brace residue) before PASS; 1 false equivalence claim "
              "(byte-identical aliases) caught by my own sha256 check "
              "pre-ship; node -e and heredoc interpreter blocked in "
              "single-query mode (script-file pattern)",
    "verified": "rig numbers all from real DOM rects (audit_d1440/m390.json, "
                "audit_team.json, polish_results.json, ghost_chip_probe); "
                "gates PASS x4 runs; live bytes fetched + diffed; vault + "
                "SOUL.md updated and read back",
}
# Trimmed-but-real shape of run 520 metadata (t_8ad00ee9): rich, traceless.
_T8AD00EE9_METADATA = {
    "ai_hero_closure": "override verified closed: zero AI assets; live==local "
                       "sha16 on wired real fv-2026 pair",
    "artifacts": ["G:\\\\WebDevProjects\\\\_t_8ad00ee9\\\\ROLLOUT_ACCEPTANCE.md"],
    "color_gate": "verify_card_dna.py: 91 hex + 126 rgb tokens PASS, zero "
                  "color violations; 7 word residuals",
    "hold_ms_note": "live HOLD_MS=1800 per dated CEO override",
    "live_checks_this_run": "alias 200; served tokens 8.16.0/6.13.0; splash "
                            "notice live; f-contact x4 served",
    "preview_url": "https://butchers-barbershop.vercel.app",
    "scope": "acceptance-only: zero writes to site/ by this lane",
    "vault_note": "10 Projects/Butchers Barbershop Redesign.md",
    "worker_session_id": "20260930_165408_bde1f6",
}
# Verbatim summary tail of run 505 (t_ba638969): summary-prose trace.
_TBA638969_SUMMARY_TAIL = (
    "Loop-closure trace: attempted=v2 re-derivation (script/css/recipe/rig) "
    "+ override fold-in + live==local byte census (3 lines/6 figure tokens, "
    "orchestrator's \"x6/2 descs\" corrected); failed=v1 rig token hardcode + "
    "strong-probe crash on removed element + one 390px vision overflow claim "
    "(debunked by CDP rects + card-crop re-look); verified=22/22 gates, "
    "override gates zero-figures with truth pins kept, card-DNA gate delta "
    "zero-new, live tree untouched; do-differently=sandbox-copy rigs + regex "
    "cache tokens + full-comment-thread re-read and content-pin "
    "re-derivation on every auto-resume."
)
# Verbatim summary of run 455 (t_030fd9d5): traceless (window-1 miss).
_T030FD9D5_SUMMARY = (
    "Staged the complete B&W card-DNA chrome foundation kit (append-only CSS "
    "block with silver/chrome tokens, header brand re-weight, rule-head "
    "flanked-header primitive, footer phone|web|email contact row styles, "
    "grayscale pole-flank utility, f-mark page modifier; apply-ready markup "
    "snippets; automated color gate) in designer-owned G:\\WebDevProjects"
    "\\butchers-redesign\\_designer_card_dna\\ \u2014 NOT applied because "
    "frontend t_c116c039 was actively rewriting + deploying the same shared "
    "lanes mid-run. Gate-verified: kit PASS, live tree PASS except "
    "documented known-allowed residuals."
)
# Verbatim summary of run 523 (t_543274e3): arrow-separated prose trace.
_T543274E3_TRACE_PROSE = (
    "Trace: attempted rotor-fix triage -> failed to find any rotor in tree "
    "(phantom) -> verified rig-artifact identity via repro rig + SSIM/YAVG "
    "+ live bytes -> do-differently: census the tree for a named "
    "defect-element and reproduce the capture rig's exact conditions BEFORE "
    "speccing any fix."
)

REAL_PAYLOADS = {
    # tid: (summary, metadata, expected_pass)
    "t_030fd9d5": (_T030FD9D5_SUMMARY, None, False),
    "t_8ad00ee9": ("Root rollout acceptance for t_8ad00ee9: all 6 child "
                   "waves plus the AI-hero CRITICAL closure verified live "
                   "on butchers-barbershop.vercel.app with my own byte "
                   "evidence.", _T8AD00EE9_METADATA, False),
    "t_3d0e8442": (_T3D0E8442_SUMMARY,
                   {"loop_closure_trace": _T3D0E8442_TRACE,
                    "deliverables": "see summary",
                    "handoffs": {"apply_card": "t_4abd5c6a"}},
                   True),
    "t_67b4d207": ("Staged the complete dual Book Now location-control "
                   "visual treatment. Zero writes to site/ (5 sibling lanes "
                   "live). Trace: attempted spec-only handoff \u2192 pivoted to "
                   "kit+gate+mock evidence; failed 3\u00d7 on gate regex "
                   "nested-CSS parsing \u2192 brace-depth scanner; verified gate "
                   "PASS \u00d73, 3-viewport rig \u00d73, vision adjudication; "
                   "do-differently: adopt-contract-own-visual-layer is the "
                   "repeatable form.", {"gates": "verify_book_loc.py PASS x3"},
                   True),
    "t_ba638969": ("Events editorial B&W kit re-derived to v2 on resume: "
                   "CEO 14:47 raffle-figures override folded in; proven "
                   "22/22 gates PASS. " + _TBA638969_SUMMARY_TAIL,
                   {"execution_trace": {"attempted": "v2 re-derivation",
                                        "failed": "v1 rig token hardcode",
                                        "verified": "22/22 gates",
                                        "do_differently": "sandbox-copy rigs"}},
                   True),
    "t_543274e3": ("Adjudicated the reported \"header location rotor "
                   "collision\" as NOT A DEFECT \u2014 a QC-rig artifact. " +
                   _T543274E3_TRACE_PROSE,
                   {"verdict": "NOT A DEFECT"}, True),
}


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
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="loop-trace-gate-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


@pytest.fixture
def designer_env(worker_env, monkeypatch):
    """worker_env but acting as the designer profile (pilot #3, t_f431ca9b)."""
    monkeypatch.setenv("HERMES_PROFILE", "designer")
    return worker_env


@pytest.fixture
def frontend_env(worker_env, monkeypatch):
    """worker_env but acting as the frontend profile (pilot #2, t_a617051b)."""
    monkeypatch.setenv("HERMES_PROFILE", "frontend")
    return worker_env


def _out_dict(out: str) -> dict:
    return json.loads(out)


# ---------------------------------------------------------------------------
# Unit: _missing_loop_trace
# ---------------------------------------------------------------------------

_GATED_PROFILES = ("default", "frontend", "designer")
_UNGATED_PROFILE = "test-worker"


class TestMissingLoopTraceUnit:
    def test_other_profiles_exempt(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", _UNGATED_PROFILE)
        assert kt._missing_loop_trace("did stuff", None, None) is None

    @pytest.mark.parametrize("profile", _GATED_PROFILES)
    def test_gated_traceless_rejected(self, monkeypatch, profile):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", profile)
        msg = kt._missing_loop_trace("did stuff, all good", None, None)
        assert msg is not None and "attempted" in msg

    @pytest.mark.parametrize("profile", _GATED_PROFILES)
    def test_gated_full_trace_passes(self, monkeypatch, profile):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", profile)
        assert kt._missing_loop_trace(TRACE, None, None) is None

    @pytest.mark.parametrize("profile", _GATED_PROFILES)
    def test_gated_partial_trace_rejected(self, monkeypatch, profile):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", profile)
        partial = "attempted x. failed y. verified z."
        msg = kt._missing_loop_trace(partial, None, None)
        assert msg is not None and "do-differently" in msg

    @pytest.mark.parametrize("profile", _GATED_PROFILES)
    def test_gated_nothing_new_escape_hatch(self, monkeypatch, profile):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", profile)
        assert kt._missing_loop_trace("ran probe. loop-closure: nothing new", None, None) is None

    def test_case_insensitive_verbs(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "designer")
        assert kt._missing_loop_trace(TRACE.upper(), None, None) is None

    def test_separator_variants_pass(self, monkeypatch):
        """hyphen / underscore / space separators are equivalent
        (window-2 audit: do_differently JSON keys)."""
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "designer")
        spaced = ("attempted things. failed once. verified twice. "
                  "do differently next time.")
        assert kt._missing_loop_trace(spaced, None, None) is None
        underscored = "attempted=a failed=b verified=c do_differently=d"
        assert kt._missing_loop_trace(underscored, None, None) is None

    def test_metadata_nested_trace_object_passes(self, monkeypatch):
        """t_3d0e8442 shape: traceless summary + loop_closure_trace metadata."""
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "designer")
        msg = kt._missing_loop_trace(_T3D0E8442_SUMMARY, None,
                                     {"loop_closure_trace": _T3D0E8442_TRACE})
        assert msg is None

    def test_metadata_execution_trace_key_passes(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "designer")
        msg = kt._missing_loop_trace("plain summary", None,
                                     {"execution_trace": _T3D0E8442_TRACE})
        assert msg is None

    def test_rich_metadata_without_trace_rejected(self, monkeypatch):
        """t_8ad00ee9 shape: rich acceptance metadata, zero trace fields."""
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "designer")
        msg = kt._missing_loop_trace(
            "Root rollout acceptance: all 6 child waves plus the AI-hero "
            "CRITICAL closure verified live with byte evidence.",
            None, _T8AD00EE9_METADATA)
        assert msg is not None

    def test_single_source_rule(self, monkeypatch):
        """Three concepts in the summary + the fourth in metadata do NOT
        count: the trace must be complete within one source."""
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "designer")
        msg = kt._missing_loop_trace(
            "attempted x. failed y. verified z.",
            None, {"note": "do-differently: split sources"})
        assert msg is not None

    def test_unprofiled_env_falls_back_to_default(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        msg = kt._missing_loop_trace("plain summary", None, None)
        assert msg is not None

    @pytest.mark.parametrize("tid, expected_pass", [
        ("t_030fd9d5", False), ("t_8ad00ee9", False), ("t_3d0e8442", True),
        ("t_67b4d207", True), ("t_ba638969", True), ("t_543274e3", True),
    ])
    def test_real_audit_payloads(self, monkeypatch, tid, expected_pass):
        """The exact window-1/2 audit evidence set (acceptance criterion b):
        t_030fd9d5 and t_8ad00ee9 must FAIL; the other four must PASS."""
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "designer")
        summary, metadata, _ = REAL_PAYLOADS[tid]
        msg = kt._missing_loop_trace(summary, None, metadata)
        assert (msg is None) is expected_pass, (
            f"{tid}: expected {'PASS' if expected_pass else 'REJECT'}, "
            f"gate said {msg!r}"
        )


# ---------------------------------------------------------------------------
# Integration: _handle_complete honours the gate
# ---------------------------------------------------------------------------

class TestCompleteGateIntegration:
    def test_designer_traceless_completion_rejected(self, designer_env):
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": "shipped the kit, looks great"})
        d = _out_dict(out)
        assert "error" in d
        assert "LOOP-CLOSURE" in d["error"]
        assert "missing" in d["error"]
        # gate fires before DB mutation -> task still in-flight
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        conn = kbc.connect()
        try:
            t = kb.get_task(conn, designer_env)
            assert t.status == "running", (
                f"gate must leave task in-flight, got {t.status}")
        finally:
            conn.close()

    def test_designer_traced_completion_succeeds(self, designer_env):
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": TRACE})
        d = _out_dict(out)
        assert d.get("ok") is True, d
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        conn = kbc.connect()
        try:
            t = kb.get_task(conn, designer_env)
            assert t.status == "done"
        finally:
            conn.close()

    def test_designer_metadata_nested_trace_succeeds(self, designer_env):
        """The t_3d0e8442 production shape: prose summary + trace object
        in metadata must complete without false rejection."""
        from tools import kanban_tools as kt
        out = kt._handle_complete({
            "summary": _T3D0E8442_SUMMARY,
            "metadata": {"loop_closure_trace": _T3D0E8442_TRACE},
        })
        d = _out_dict(out)
        assert d.get("ok") is True, d

    def test_designer_rich_metadata_traceless_rejected(self, designer_env):
        """The t_8ad00ee9 production shape must be refused end-to-end."""
        from tools import kanban_tools as kt
        out = kt._handle_complete({
            "summary": "Root rollout acceptance: all 6 child waves verified "
                       "live with byte evidence.",
            "metadata": _T8AD00EE9_METADATA,
        })
        d = _out_dict(out)
        assert "error" in d and "LOOP-CLOSURE" in d["error"], d
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        conn = kbc.connect()
        try:
            t = kb.get_task(conn, designer_env)
            assert t.status == "running"
        finally:
            conn.close()

    def test_designer_nothing_new_completion_succeeds(self, designer_env):
        from tools import kanban_tools as kt
        out = kt._handle_complete({
            "summary": "css re-check. loop-closure: nothing new"})
        d = _out_dict(out)
        assert d.get("ok") is True, d

    def test_worker_profile_unaffected(self, worker_env):
        """worker_env sets HERMES_PROFILE=test-worker — no gate."""
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": "plain worker summary"})
        d = _out_dict(out)
        assert d.get("ok") is True, d

    def test_frontend_traceless_completion_rejected(self, frontend_env):
        """Pilot #2 behaviour unchanged (criterion c)."""
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": "shipped the section"})
        d = _out_dict(out)
        assert "error" in d and "LOOP-CLOSURE" in d["error"], d

    def test_default_traceless_completion_rejected(self, worker_env, monkeypatch):
        """Default-profile behaviour unchanged (criterion c)."""
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "default")
        out = kt._handle_complete({"summary": "all done, shipped it"})
        d = _out_dict(out)
        assert "error" in d and "LOOP-CLOSURE" in d["error"], d
