"""Near-identical tool-call watchdog (run-244 wedge class).

Evidence (debugger t_3df0dd33, session 20260901_090822_43a122, run 244 on
t_a617051b): a live worker polled a hung background pytest with
``process(action="wait")`` 76 times over ~3.9 hours. 72/76 calls were
byte-identical; the other 4 differed ONLY in the ``timeout`` numeric
(170→175→178). The existing anti-wedge result note ("This is not an error",
``process_running:true``) was present 75/76 times and ignored every time —
result-text mitigation alone cannot break this trance. Assistant text was
EMPTY in 68/68 wedge-window turns, and ``max_iterations`` defaults to
``sys.maxsize``, so nothing bounded the loop.

The existing guards cover adjacent failure modes but not this one:

- ``agent.empty_response_guard`` — provider empties, not live-model loops.
- ``agent/turn_liveness.py`` — silent in-turn stalls, not live loops.
- ``agent/tool_guardrails.py`` identical-call breaker — fires only when the
  RESULT is byte-identical too, and explicitly EXEMPTS pollers like
  ``process`` (``STALL_GUARD_REPEATABLE_TOOLS``). The wedge class here is
  exactly a poller whose volatile args (uptime counters inside output, the
  drifting ``timeout``) defeat byte-level comparison.

This module is policy-only (following ``agent/kanban_stop.py``): it computes
signatures and nudge text. The executor seam observes calls into it and the
conversation loop consumes the nudge/force-end decisions.

Signature normalization: tool name + canonical JSON of arguments with values
under VOLATILE_ARG_KEYS dropped (``timeout`` drifted 170→175→178 in the
incident; a byte-identical comparison would fire late or never). Target-
selecting numerics (``offset``, ``start_line``, ``limit``, ...) are KEPT, so
paging through a file or two different reads remain distinct calls and the
watchdog never fires on them.

False-positive safety (spec item 3):

- Legitimate repeats of idempotent reads rarely reach 5+ CONSECUTIVE
  near-identical calls with no other tool in between (a streak reset), and
  the nudge is advisory text — the model can keep going if it is genuinely
  making progress.
- Deliberate retry after a transient error: the retry conventionally changes
  args or is interleaved with other diagnostic calls; even a pure identical
  retry streak only earns a nudge at streak 5, then a second nudge, before
  any force-end at streak 11.
- Poll/log alternation: alternating calls reset the streak every other call,
  so the streak never accumulates.

Streak lifecycle: reset whenever ANY different call is observed, and reset
per turn (``agent/turn_context.py`` calls ``reset_for_turn`` alongside the
other tool guardrails) so one turn's wedge cannot poison the next.

Config (``agent.repetition_watchdog`` in config.yaml)::

    agent:
      repetition_watchdog:
        enabled: true     # false = feature off entirely
        max_streak: 5     # first nudge at this consecutive-identical count
        max_nudges: 2     # nudges before force-end; force-end at
                          # max_streak + max_nudges * nudge_grace
        nudge_grace: 3    # calls between nudges / before force-end

Force-end (run-244's "nothing bounded the loop"): after the nudge budget is
exhausted the streak persisting means the model cannot break out on its own;
the watchdog asks the conversation loop to end the turn with a clear finish
reason so the worker surfaces (dispatcher sees the exit) instead of burning
hours.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# Argument keys whose VALUES are dropped before hashing. These select
# *durations/counts of waiting*, not *what* is being called. The run-244
# incident drifted only ``timeout`` (170→175→178) across 76 calls.
# ``session_id``/``action`` are KEPT — they select the target.
#
# Deliberately conservative: keys here must never disambiguate two logically
# different calls. timeout/wait/delay/interval/retries/limit are wait-shaping
# or retry-budget knobs. `limit` is included because a poller that changes
# only its output cap is still polling the same thing; tools where limit
# selects a different window (read_file offset+limit) keep `offset`/`start`
# in the signature, so paging stays distinct.
VOLATILE_ARG_KEYS = frozenset(
    {
        "timeout",
        "wait",
        "wait_timeout",
        "timeout_s",
        "timeout_seconds",
        "seconds",
        "delay",
        "interval",
        "poll_interval",
        "retries",
        "max_retries",
        "retry",
        "attempt",
        "attempts",
        "limit",
        "max",
        "max_results",
        "page_size",
        "offset_timeout",
    }
)

# Canonical nudge thresholds (spec suggestions).
DEFAULT_ENABLED = True
DEFAULT_MAX_STREAK = 5
DEFAULT_MAX_NUDGES = 2
DEFAULT_NUDGE_GRACE = 3

# Finish reason the loop stamps when force-ending the turn.
REPETITION_WATCHDOG_FINISH_REASON = "repetition_watchdog_force_end"


@dataclass(frozen=True)
class WatchdogConfig:
    enabled: bool = DEFAULT_ENABLED
    max_streak: int = DEFAULT_MAX_STREAK
    max_nudges: int = DEFAULT_MAX_NUDGES
    nudge_grace: int = DEFAULT_NUDGE_GRACE

    @property
    def force_end_streak(self) -> int:
        """Streak count at which the turn is force-ended."""
        if self.max_nudges <= 0:
            return self.max_streak
        return self.max_streak + self.max_nudges * self.nudge_grace


@dataclass
class WatchdogDecision:
    """Result of observing one call against the streak."""

    action: str = "none"  # none | nudge | force_end
    streak: int = 0
    nudge_text: Optional[str] = None
    tool_name: str = ""
    args_preview: str = ""


@dataclass
class RepetitionWatchdogState:
    """Rolling near-identical-call tracker. One instance per agent."""

    config: WatchdogConfig = field(default_factory=WatchdogConfig)
    _signature: Optional[str] = None
    _streak: int = 0
    _nudges_issued: int = 0
    _last_nudge_streak: int = 0
    _force_ended: bool = False
    _last_call_desc: str = ""

    # ------------------------------------------------------------------
    # Config resolution (config.yaml agent.repetition_watchdog, tolerant)
    # ------------------------------------------------------------------
    @classmethod
    def from_config_section(cls, section: Any) -> "RepetitionWatchdogState":
        cfg = WatchdogConfig()
        if isinstance(section, Mapping):
            enabled_raw = section.get("enabled", cfg.enabled)
            if isinstance(enabled_raw, str):
                enabled = enabled_raw.strip().lower() not in {
                    "0", "false", "no", "off",
                }
            elif isinstance(enabled_raw, bool):
                enabled = enabled_raw
            else:
                enabled = cfg.enabled
            max_streak = _positive_int(section.get("max_streak"), cfg.max_streak)
            max_nudges = _non_negative_int(section.get("max_nudges"), cfg.max_nudges)
            nudge_grace = _positive_int(section.get("nudge_grace"), cfg.nudge_grace)
            cfg = WatchdogConfig(
                enabled=enabled,
                max_streak=max_streak,
                max_nudges=max_nudges,
                nudge_grace=nudge_grace,
            )
        return cls(config=cfg)

    # ------------------------------------------------------------------
    # Signature
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_args(args: Mapping[str, Any] | None) -> str:
        """Canonical JSON of args with volatile values dropped (recursively)."""
        cleaned = _drop_volatile(args if isinstance(args, Mapping) else {})
        return json.dumps(
            cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )

    @classmethod
    def signature(cls, tool_name: str, args: Mapping[str, Any] | None) -> str:
        canonical = cls.normalize_args(args)
        digest = hashlib.sha256(
            f"{tool_name}\x00{canonical}".encode("utf-8", "surrogatepass")
        ).hexdigest()
        return digest

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------
    def reset_for_turn(self) -> None:
        """Clear per-turn state (called from agent/turn_context.py)."""
        self._signature = None
        self._streak = 0
        self._nudges_issued = 0
        self._last_nudge_streak = 0
        self._force_ended = False
        self._last_call_desc = ""

    def observe(self, tool_name: str, args: Mapping[str, Any] | None) -> WatchdogDecision:
        """Observe one about-to-dispatch tool call; update the streak.

        Call this at the tool-executor seam BEFORE dispatch. Any call with a
        different normalized signature resets the streak, so alternation
        (poll/log, poll/read) and arg changes (different file, different
        session_id) never accumulate.
        """
        if not self.config.enabled:
            return WatchdogDecision()

        sig = self.signature(tool_name, args)
        if sig == self._signature:
            self._streak += 1
        else:
            self._signature = sig
            self._streak = 1
            self._nudges_issued = 0
            self._last_nudge_streak = 0
            self._force_ended = False

        self._last_call_desc = f"{tool_name}({self.normalize_args(args)})"

        if self._streak < self.config.max_streak:
            return WatchdogDecision(
                action="none", streak=self._streak, tool_name=tool_name
            )

        # Past the threshold: nudge, then escalate to force-end.
        if self.config.max_nudges > 0 and self._nudges_issued < self.config.max_nudges:
            due = (
                self._last_nudge_streak == 0
                or self._streak - self._last_nudge_streak >= self.config.nudge_grace
            )
            if due:
                self._nudges_issued += 1
                self._last_nudge_streak = self._streak
                return WatchdogDecision(
                    action="nudge",
                    streak=self._streak,
                    tool_name=tool_name,
                    args_preview=self._preview_args(args),
                    nudge_text=build_repetition_nudge(
                        tool_name=tool_name,
                        args_preview=self._preview_args(args),
                        streak=self._streak,
                        nudge_number=self._nudges_issued,
                        max_nudges=self.config.max_nudges,
                    ),
                )
            return WatchdogDecision(
                action="none", streak=self._streak, tool_name=tool_name
            )

        # Nudge budget exhausted and the streak persists: force-end.
        if (
            self._streak
            >= self.config.max_streak + self.config.max_nudges * self.config.nudge_grace
        ):
            self._force_ended = True
            return WatchdogDecision(
                action="force_end",
                streak=self._streak,
                tool_name=tool_name,
                args_preview=self._preview_args(args),
            )

        return WatchdogDecision(action="none", streak=self._streak, tool_name=tool_name)

    @property
    def force_ended(self) -> bool:
        return self._force_ended

    @staticmethod
    def _preview_args(args: Mapping[str, Any] | None) -> str:
        try:
            preview = RepetitionWatchdogState.normalize_args(args)
        except Exception:
            preview = "{}"
        if len(preview) > 200:
            preview = preview[:200] + "…"
        return preview


def build_repetition_nudge(
    *,
    tool_name: str,
    args_preview: str,
    streak: int,
    nudge_number: int,
    max_nudges: int,
) -> str:
    """Synthetic system nudge naming the exact repeated call (bounded)."""
    remaining = max_nudges - nudge_number
    escalation = (
        "If it repeats again after this, the turn will be ended automatically."
        if remaining <= 0
        else f"This is automatic nudge {nudge_number} of {max_nudges}."
    )
    return (
        "[System: repetition watchdog — you have issued the SAME tool call "
        f"{streak} times in a row:\n"
        f"  {tool_name} with arguments {args_preview}\n"
        "(wait/timeout numerics are ignored in this comparison). Re-issuing "
        "it is not making progress; the result is not going to change.\n\n"
        "Change strategy NOW, in your next response:\n"
        "1. If you are waiting on a background process: kill it "
        "(process(action=\"kill\") or session cleanup), collect its logs "
        "(process(action=\"log\")), and proceed with what you have.\n"
        "2. If a call keeps failing: do a small different diagnostic, then "
        "pick a different tool or different arguments.\n"
        "3. If you have enough information: stop calling tools and produce "
        "the deliverable / report the blocker.\n"
        f"{escalation}]"
    )


def _drop_volatile(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): _drop_volatile(v)
            for k, v in value.items()
            if str(k) not in VOLATILE_ARG_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_drop_volatile(v) for v in value]
    return value


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _non_negative_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


__all__ = [
    "REPETITION_WATCHDOG_FINISH_REASON",
    "RepetitionWatchdogState",
    "WatchdogConfig",
    "WatchdogDecision",
    "build_repetition_nudge",
]
