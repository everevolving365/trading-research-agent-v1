"""Session narration, the morning brief and the evening debrief (66, 67).

During the trading day the agent talks: setups forming, conditions met, what it
is waiting on. All of it is computed from the same compiled plan the engine and
the live path use, so the narration can never describe a strategy other than the
one that is actually running.

Everything here returns text. The voice shell speaks it; with voice off it is
printed. Nothing is gated behind a microphone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from ee_agent.compile.to_python import compile_to_python
from ee_agent.spec.model import StrategySpec


@dataclass
class NarrationEvent:
    ts: str
    kind: str  # arming | forming | fired | waiting | invalidated | flat | blackout
    text: str
    bar_index: int = -1

    def line(self) -> str:
        return f"{self.ts[11:19]}  {self.text}"


class SessionNarrator:
    """Turns the plan's internal state into sentences a person can follow."""

    def __init__(self, spec: StrategySpec, sink=print):
        self.spec = spec
        self.sink = sink
        self.compiled = compile_to_python(spec)
        self.events: list[NarrationEvent] = []
        self._last_state: dict[str, bool] = {}

    def narrate_bar(self, bars, i: int, signals=None) -> list[NarrationEvent]:
        """One bar's worth of commentary. Only says something when something changed."""
        signals = signals if signals is not None else self.compiled.evaluate(bars)
        ts = bars.df["ts"].iloc[i].isoformat()
        out: list[NarrationEvent] = []

        def changed(key: str, value: bool) -> bool:
            was = self._last_state.get(key)
            self._last_state[key] = value
            return was is not None and was != value and value

        in_window = bool(signals.filter_ok[i])
        if changed("in_window", in_window):
            window = self.spec.filters.time_windows[0] if self.spec.filters.time_windows else None
            out.append(
                NarrationEvent(ts, "waiting", f"The signal window is open ({window.start}-{window.end})."
                              if window else "The signal window is open.", i)
            )

        for key, array in signals.debug.items():
            if key.startswith("context:") and key.endswith(".complete"):
                name = key.split(":")[1].split(".")[0]
                if changed(key, bool(array[i])):
                    high = signals.debug.get(f"context:{name}.high")
                    low = signals.debug.get(f"context:{name}.low")
                    detail = ""
                    if high is not None and low is not None and not np.isnan(high[i]):
                        detail = f" -- high {high[i]:,.2f}, low {low[i]:,.2f}"
                    out.append(
                        NarrationEvent(ts, "forming", f"The {name.replace('_', ' ')} is set{detail}.", i)
                    )

        for key, array in signals.debug.items():
            if key.startswith("entry:"):
                continue
        armed_keys = [k for k in signals.debug if "range_break" in k or k.startswith("scratch:armed")]
        for key in armed_keys:
            if changed(key, bool(signals.debug[key][i])):
                out.append(NarrationEvent(ts, "arming", "Price has taken one side of the range. "
                                          "I am watching for it to come back inside.", i))

        if signals.long_entry[i] or signals.short_entry[i]:
            side = "long" if signals.long_entry[i] else "short"
            out.append(
                NarrationEvent(
                    ts, "fired",
                    f"Signal: {side.upper()} at {bars.close[i]:,.2f}. "
                    f"{'Stop ' + str(self.spec.risk.stop.value) + ' ' + self.spec.risk.stop.type if self.spec.risk.stop else ''}"
                    f"{', target ' + str(self.spec.risk.target.value) + ' ' + self.spec.risk.target.type if self.spec.risk.target else ''}.",
                    i,
                )
            )
        if signals.invalidation[i]:
            out.append(NarrationEvent(ts, "invalidated", "The window is closing -- flattening anything open.", i))

        self.events.extend(out)
        for event in out:
            self.sink(event.line())
        return out

    def narrate_session(self, bars, start: int = 0, end: int | None = None) -> list[NarrationEvent]:
        signals = self.compiled.evaluate(bars)
        end = end if end is not None else len(bars)
        collected: list[NarrationEvent] = []
        for i in range(start, end):
            collected.extend(self.narrate_bar(bars, i, signals))
        return collected

    def waiting_on(self, bars, i: int, signals=None) -> str:
        """"What is it waiting on?" -- answered from the plan, not a guess."""
        signals = signals if signals is not None else self.compiled.evaluate(bars)
        if not signals.filter_ok[i]:
            windows = self.spec.filters.time_windows
            return (
                f"Outside the signal window; it opens at {windows[0].start}." if windows else "Outside the window."
            )
        for key, array in signals.debug.items():
            if key.endswith(".complete") and not array[i]:
                name = key.split(":")[1].split(".")[0]
                return f"The {name.replace('_', ' ')} is still building. Nothing can fire until it is set."
        armed = [k for k in signals.debug if "range_break" in k and signals.debug[k][i]]
        if armed:
            return "The range has been broken. I am waiting for price to close back inside."
        return "In the window, range is set, waiting for price to take one side."


def morning_brief(
    spec: StrategySpec,
    calendar=None,
    overnight=None,
    research_summary: str = "",
    ledger_summary: str = "",
    spend_line: str = "",
) -> str:
    """Spoken at the start of the day (ability 67)."""
    from datetime import date

    lines = [f"Good morning. It is {date.today().strftime('%A %d %B %Y')}."]
    lines.append(f"Today I am running {spec.name} on {', '.join(spec.universe.instruments)}.")
    if spec.risk.stop and spec.risk.target:
        lines.append(
            f"Your rules, unchanged: stop {spec.risk.stop.value} {spec.risk.stop.type}, "
            f"target {spec.risk.target.value} {spec.risk.target.type}, "
            f"{spec.risk.size.value:g} by {spec.risk.size.method.replace('_', ' ')}."
        )
    if spec.filters.time_windows:
        window = spec.filters.time_windows[0]
        lines.append(f"The signal window is {window.start} to {window.end} {window.tz or spec.universe.timezone}.")
    if calendar is not None:
        from datetime import date as _date

        today = calendar.on(_date.today())
        if today:
            lines.append("Scheduled today: " + "; ".join(f"{e.name} at {e.time_local}" for e in today) + ".")
            lines.append("I will stay out of the blackout windows around those.")
        else:
            lines.append("Nothing scheduled on the economic calendar today -- which is not the same as nothing happening.")
    if overnight is not None:
        lines.append("")
        lines.append(overnight.morning_brief())
    for extra in (research_summary, ledger_summary, spend_line):
        if extra:
            lines.append(extra)
    unresolved = spec.unresolved_assumptions()
    if unresolved:
        lines.append(
            f"One thing before we start: {len(unresolved)} assumption(s) are still unapproved, "
            "so this cannot trade live until you confirm them."
        )
    return "\n".join(lines)


def evening_debrief(
    spec: StrategySpec,
    trades: list,
    drift=None,
    scorecard=None,
    spend_line: str = "",
    shadow=None,
) -> str:
    """Spoken at the end of the day. Honest about a losing day."""
    closed = [t for t in trades if getattr(t, "closed", False)]
    pnl = sum(getattr(t, "net_pnl", getattr(t, "pnl", 0.0)) for t in closed)
    wins = [t for t in closed if getattr(t, "net_pnl", getattr(t, "pnl", 0.0)) > 0]
    lines = [f"Evening debrief for {spec.name}."]
    if not closed:
        lines.append("No trades today. The setup did not appear, which is a result and not a failure.")
    else:
        lines.append(
            f"{len(closed)} trade(s), {len(wins)} winner(s), net {pnl:,.2f}."
        )
        for t in closed[:6]:
            side = getattr(t, "side", "?")
            reason = getattr(t, "exit_reason", "")
            value = getattr(t, "net_pnl", getattr(t, "pnl", 0.0))
            lines.append(f"    {side:<5} closed by {reason or 'exit':<16} {value:>10,.2f}")
    if drift is not None:
        lines.append(drift.summary())
    if scorecard is not None:
        lines.append(scorecard.summary())
    if shadow is not None:
        lines.append(shadow.summary())
    if spend_line:
        lines.append(spend_line)
    lines.append(
        "Nothing here is a defensible number on its own -- one day is one window. "
        "The research index keeps the long run."
    )
    return "\n".join(lines)
