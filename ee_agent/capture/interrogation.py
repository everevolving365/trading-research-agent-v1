"""The interrogation engine (ability 10) and the ambiguity ledger (ability 13).

The agent questions the strategy rather than accepting it. It works through a
completeness checklist -- entry trigger, confirmation, invalidation, stop
placement, target logic, session and time filters, maximum trades per day,
behaviour when two signals overlap, behaviour on gaps, behaviour when a position
is open at session close, and whether the strategy scales in -- and it does not
proceed until every item is answered by the client or logged as an approved
assumption.

It never answers for the client. An unanswered item becomes an assumption with
``approved_by: pending``, which the validator treats as blocking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ee_agent.spec.model import Assumption, Condition, Distance, Sizing, StrategySpec
from ee_agent.spec.validator import COMPLETENESS_CHECKLIST, checklist_status, validate


@dataclass
class Question:
    topic: str
    text: str
    why: str = ""
    options: list[str] = field(default_factory=list)
    default_assumption: str = ""

    def ask_text(self) -> str:
        lines = [self.text]
        if self.why:
            lines.append(f"  (I need this because {self.why})")
        if self.options:
            lines.append("  e.g. " + " / ".join(self.options))
        return "\n".join(lines)


#: One question per checklist item, plus what it is for and what the agent will
#: record as an assumption if the client declines to answer.
QUESTIONS: dict[str, Question] = {
    "entry_trigger": Question(
        "entry_trigger",
        "What exactly has to happen for you to enter? Describe the bar you would point at.",
        "without this there is no strategy to compile",
        ["price breaks the range high then closes back inside", "the 9 EMA crosses above the 21"],
    ),
    "confirmation": Question(
        "confirmation",
        "Do you act the moment it happens, or wait for the candle to close?",
        "this changes every fill price and is the single most common source of a backtest that "
        "cannot be traded",
        ["wait for the close", "act immediately"],
        "wait for the bar close",
    ),
    "invalidation": Question(
        "invalidation",
        "What tells you the setup is dead, before your stop is hit?",
        "without it the trade only ends at the stop or the target, which is rarely how people trade",
        ["the session window ends", "price closes back outside the range"],
        "the signal window ends",
    ),
    "stop_placement": Question(
        "stop_placement",
        "Where does the stop go? Give me a distance, not a price.",
        "I will not run a backtest with no stop",
        ["25 points", "1.5 ATR", "the other side of the range"],
    ),
    "target_logic": Question(
        "target_logic",
        "Where do you take profit? Or is there a trail instead?",
        "something has to close a winning trade",
        ["50 points", "2R", "trail by 1 ATR"],
    ),
    "session_filter": Question(
        "session_filter",
        "Which hours does this trade, and in which timezone?",
        "session windows decide which bars are even eligible",
        ["09:30 to 15:00 Central", "the regular session only"],
    ),
    "max_trades_per_day": Question(
        "max_trades_per_day",
        "How many times a day will you take this?",
        "it is the difference between a strategy and a machine that grinds costs",
        ["two", "as many as fire"],
        "no limit",
    ),
    "overlapping_signals": Question(
        "overlapping_signals",
        "If a second signal fires while you are already in a trade, what happens?",
        "the three answers give measurably different results",
        ["ignore it", "replace the position", "add to it"],
        "ignore the new signal",
    ),
    "gap_behavior": Question(
        "gap_behavior",
        "If price gaps straight through your level overnight, do you take the trade at the open or skip it?",
        "gaps are where an unrealistic backtest hides",
        ["fill at the open", "skip it"],
        "fill at the open",
    ),
    "session_close_behavior": Question(
        "session_close_behavior",
        "If you are still in a trade when the window ends, do you flatten or hold?",
        "holding overnight changes the risk completely, and most prop firms forbid it",
        ["flatten", "hold"],
        "flatten at the end of the window",
    ),
    "scale_in": Question(
        "scale_in",
        "Do you ever add to a position that is already open?",
        "scaling in changes position sizing and the drawdown profile",
        ["no", "yes, once"],
        "no scaling in",
    ),
}


@dataclass
class InterrogationState:
    spec: StrategySpec
    asked: list[str] = field(default_factory=list)
    answered: dict[str, str] = field(default_factory=dict)
    assumed: list[str] = field(default_factory=list)

    @property
    def outstanding(self) -> list[Question]:
        status = checklist_status(self.spec)
        return [QUESTIONS[key] for key, _ in COMPLETENESS_CHECKLIST if not status.get(key, False)]

    @property
    def complete(self) -> bool:
        return not self.outstanding and not self.spec.unresolved_assumptions()


class InterrogationEngine:
    """Drives the questioning. ``answer_fn`` is the client -- a voice loop, a
    CLI prompt, or a fixture in tests."""

    def __init__(self, spec: StrategySpec, sink: Callable[[str], None] = print):
        self.state = InterrogationState(spec=spec)
        self.sink = sink

    @property
    def spec(self) -> StrategySpec:
        return self.state.spec

    def next_question(self) -> Question | None:
        outstanding = self.state.outstanding
        return outstanding[0] if outstanding else None

    def run(self, answer_fn: Callable[[Question], str | None], max_rounds: int = 30) -> StrategySpec:
        """Ask until the checklist is complete. An unanswered item is recorded as
        an assumption with the client's approval left PENDING -- never faked."""
        rounds = 0
        while rounds < max_rounds:
            question = self.next_question()
            if question is None:
                break
            rounds += 1
            self.state.asked.append(question.topic)
            self.sink(question.ask_text())
            answer = answer_fn(question)
            if answer is None or not str(answer).strip():
                self._assume(question)
                continue
            self.state.answered[question.topic] = str(answer)
            applied = self.apply(question.topic, str(answer))
            if not applied:
                # understood as text but not mappable to a field: record it as an
                # assumption so nothing is silently dropped
                self._record_assumption(question, str(answer), approved_by="client")
        return self.spec

    # ------------------------------------------------------------ applying
    def apply(self, topic: str, answer: str) -> bool:
        """Map a plain-language answer onto the spec. Returns False when the
        answer was not recognised, so the caller can log it rather than lose it."""
        from ee_agent.capture.parser import find_distance, find_time_windows, find_timezone

        spec = self.spec
        lowered = answer.lower().strip()

        if topic == "stop_placement":
            dist, _ = find_distance("stop " + answer, "stop")
            if dist:
                spec.risk.stop = dist
                return True
            return False

        if topic == "target_logic":
            dist, _ = find_distance("target " + answer, "target")
            if dist:
                spec.risk.target = dist
                return True
            if "trail" in lowered:
                dist, _ = find_distance("trail " + answer, "trail")
                if dist:
                    spec.risk.trail = dist
                    return True
            return False

        if topic == "session_filter":
            windows = find_time_windows(answer)
            if windows:
                from ee_agent.spec.model import TimeWindow

                tz = find_timezone(answer) or spec.universe.timezone
                start, end, _ = windows[0]
                spec.filters.time_windows = [TimeWindow(start=start, end=end, tz=tz)]
                spec.universe.timezone = tz
                return True
            return False

        if topic == "max_trades_per_day":
            import re

            m = re.search(r"\b(\d+)\b", lowered)
            words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
            for word, value in words.items():
                if word in lowered:
                    spec.filters.max_trades_per_day = value
                    return True
            if m:
                spec.filters.max_trades_per_day = int(m.group(1))
                return True
            if any(w in lowered for w in ("no limit", "unlimited", "as many")):
                spec.filters.max_trades_per_day = 99
                return True
            return False

        if topic == "overlapping_signals":
            if "ignore" in lowered:
                spec.execution.on_overlapping_signal = "ignore_new"
            elif "replace" in lowered or "flip" in lowered:
                spec.execution.on_overlapping_signal = "replace"
            elif "add" in lowered or "stack" in lowered:
                spec.execution.on_overlapping_signal = "stack"
            else:
                return False
            return True

        if topic == "gap_behavior":
            if "skip" in lowered or "don't" in lowered or "do not" in lowered:
                spec.execution.gap_behavior = "skip"
            else:
                spec.execution.gap_behavior = "fill_at_open"
            return True

        if topic == "session_close_behavior":
            spec.execution.session_close_behavior = "hold" if "hold" in lowered else "flatten"
            if spec.execution.session_close_behavior == "flatten" and not spec.signals.invalidation:
                spec.signals.invalidation.append(Condition("session_end", {"reference": "signal_window"}))
            return True

        if topic == "scale_in":
            spec.risk.scale_in = not any(w in lowered for w in ("no", "never", "don't", "do not"))
            return True

        if topic == "confirmation":
            spec.execution.bar_close_confirmation = not any(
                w in lowered for w in ("immediate", "instantly", "straight away", "as soon", "don't wait")
            )
            return True

        if topic == "invalidation":
            if not spec.signals.invalidation:
                spec.signals.invalidation.append(Condition("session_end", {"reference": "signal_window"}))
            self._record_assumption(QUESTIONS[topic], answer, approved_by="client")
            return True

        if topic == "entry_trigger":
            from ee_agent.capture.parser import parse

            parsed = parse(answer, strategy_id=spec.id, name=spec.name)
            if parsed.spec.signals.entry:
                spec.signals.entry = parsed.spec.signals.entry
                if parsed.spec.context and not spec.context:
                    spec.context = parsed.spec.context
                return True
            return False

        return False

    # --------------------------------------------------------- assumptions
    def _assume(self, question: Question) -> None:
        """The client did not answer. Record the assumption, leave it PENDING,
        and say so out loud. It blocks live trading until approved."""
        self._record_assumption(question, question.default_assumption or "unanswered", approved_by="pending")
        if question.default_assumption:
            self.apply(question.topic, question.default_assumption)
        self.state.assumed.append(question.topic)
        self.sink(
            f"  [assumption] you did not answer, so I have written down "
            f"'{question.default_assumption or 'unanswered'}' and marked it UNAPPROVED. "
            "It will block live trading until you confirm it."
        )

    def _record_assumption(self, question: Question, resolution: str, approved_by: str) -> None:
        existing = next((a for a in self.spec.assumptions if a.id == question.topic), None)
        sensitivity = SENSITIVITY.get(question.topic, {})
        if existing:
            existing.resolution = resolution
            existing.approved_by = approved_by
            return
        self.spec.assumptions.append(
            Assumption(
                id=question.topic,
                question=question.text,
                resolution=resolution,
                approved_by=approved_by,
                alternatives=question.options,
                sensitivity=sensitivity,
            )
        )

    # -------------------------------------------------------------- report
    def ledger_report(self) -> str:
        """The ambiguity ledger, shown to the client: every assumption, and what
        changes if it is wrong."""
        if not self.spec.assumptions:
            return "[ambiguity] nothing assumed: every checklist item was answered."
        lines = ["[ambiguity] every assumption I am carrying, and what changes if it is wrong:"]
        for a in self.spec.assumptions:
            mark = "approved" if a.approved else "*** UNAPPROVED ***"
            lines.append(f"    [{a.id}] {mark}")
            lines.append(f"        Q: {a.question}")
            lines.append(f"        A: {a.resolution}")
            if a.sensitivity.get("if_wrong"):
                lines.append(f"        if wrong: {a.sensitivity['if_wrong']}")
            if a.sensitivity.get("measured"):
                lines.append(f"        measured: {a.sensitivity['measured']}")
        return "\n".join(lines)

    def status(self) -> str:
        report = validate(self.spec)
        outstanding = self.state.outstanding
        lines = [
            f"[interrogation] {len(COMPLETENESS_CHECKLIST) - len(outstanding)} of "
            f"{len(COMPLETENESS_CHECKLIST)} checklist items answered"
        ]
        for q in outstanding:
            lines.append(f"    still to ask: {q.topic}")
        if report.blocking:
            lines.append(f"    {len(report.blocking)} blocking finding(s) before this can trade live")
        return "\n".join(lines)


#: What changes if an assumption is wrong. Ability 13 asks for this explicitly.
SENSITIVITY: dict[str, dict] = {
    "confirmation": {
        "if_wrong": "Acting intrabar instead of at the close changes every entry price and usually "
        "improves the backtest in a way that does not survive live."
    },
    "invalidation": {
        "if_wrong": "Without the right invalidation the strategy holds losers to the stop that it "
        "would have exited early, which widens the loss distribution."
    },
    "max_trades_per_day": {
        "if_wrong": "A limit that is too high multiplies cost drag; too low and you miss the best setup."
    },
    "overlapping_signals": {
        "if_wrong": "Replacing instead of ignoring can double the trade count and flip the result's sign."
    },
    "gap_behavior": {
        "if_wrong": "Taking gap-through entries at the open is where an unrealistic backtest hides."
    },
    "session_close_behavior": {
        "if_wrong": "Holding overnight adds gap risk the backtest never measured, and most prop firms "
        "forbid it outright."
    },
    "scale_in": {"if_wrong": "Scaling in changes position size and therefore the entire drawdown profile."},
}


def measure_sensitivity(spec: StrategySpec, bars, assumption_id: str, alternative: str) -> dict:
    """Ability 13's harder half: not just log the assumption, MEASURE it.

    Runs the strategy both ways and reports the difference in the numbers.
    """
    from ee_agent.engine.backtester import Backtester

    variant = StrategySpec.from_dict(spec.to_dict())
    engine = InterrogationEngine(variant, sink=lambda _msg: None)
    if not engine.apply(assumption_id, alternative):
        return {"assumption": assumption_id, "error": f"could not apply the alternative {alternative!r}"}
    try:
        base = Backtester(spec).run(bars, window_label="assumption-base")
        other = Backtester(variant).run(bars, window_label=f"assumption-{assumption_id}")
    except Exception as exc:
        return {"assumption": assumption_id, "error": str(exc)}
    return {
        "assumption": assumption_id,
        "alternative": alternative,
        "base_net_pnl": round(base.metrics.net_pnl, 2),
        "alternative_net_pnl": round(other.metrics.net_pnl, 2),
        "difference": round(other.metrics.net_pnl - base.metrics.net_pnl, 2),
        "base_trades": base.metrics.n_trades,
        "alternative_trades": other.metrics.n_trades,
        "measured": (
            f"if this assumption is wrong the net result changes by "
            f"{other.metrics.net_pnl - base.metrics.net_pnl:,.2f} over this window "
            f"({base.metrics.n_trades} trades vs {other.metrics.n_trades})"
        ),
    }
