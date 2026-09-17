"""Automatic lookahead detection on every run (ability 36).

The test is direct rather than heuristic: recompute the signals using only the
bars that existed at the time, and compare. If a signal at bar *i* changes when
bars after *i* are removed, the strategy used information it could not have had.

A second, independent check compares trade entry and exit prices against the
bar's own range: a fill outside the bar that produced it is a leak.

Both run on every backtest. A strategy that peeks is flagged loudly, not
silently corrected.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ee_agent.compile.to_python import CompiledStrategy
from ee_agent.data.bars import Bars


@dataclass
class LookaheadReport:
    checked_indices: list[int] = field(default_factory=list)
    divergences: list[dict] = field(default_factory=list)
    impossible_fills: list[dict] = field(default_factory=list)
    truncation_points: int = 0
    verdict: str = "clean"
    notes: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.verdict == "clean"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "truncation_points": self.truncation_points,
            "n_divergences": len(self.divergences),
            "n_impossible_fills": len(self.impossible_fills),
            "divergences": self.divergences[:20],
            "impossible_fills": self.impossible_fills[:20],
            "notes": self.notes,
        }

    def summary(self) -> str:
        if self.clean:
            return (
                f"[lookahead] CLEAN -- {self.truncation_points} truncation point(s) re-evaluated, "
                "every signal identical with the future removed."
            )
        lines = [f"[lookahead] *** {self.verdict.upper()} ***"]
        for d in self.divergences[:5]:
            lines.append(
                f"    bar {d['index']} ({d['time']}): {d['field']} was {d['with_future']} "
                f"with the full series, {d['without_future']} without it"
            )
        for f in self.impossible_fills[:5]:
            lines.append(
                f"    trade {f['trade']}: {f['field']} {f['price']} is outside bar "
                f"{f['index']} range [{f['low']}, {f['high']}]"
            )
        lines.append("    This strategy is peeking. The reported numbers are not achievable.")
        return "\n".join(lines)


def detect(
    compiled: CompiledStrategy,
    bars: Bars,
    instrument=None,
    sample: int = 24,
    seed: int = 17,
    min_bars: int = 200,
) -> LookaheadReport:
    """Re-evaluate at ``sample`` truncation points and compare, bar for bar."""
    report = LookaheadReport()
    n = len(bars)
    if n < min_bars + 10:
        report.notes.append(f"only {n} bars: truncation test needs at least {min_bars + 10}")
        return report

    full = compiled.evaluate(bars, instrument)
    rng = np.random.default_rng(seed)
    cuts = sorted(set(int(x) for x in rng.integers(min_bars, n, size=sample)))
    report.truncation_points = len(cuts)

    for cut in cuts:
        partial_bars = bars.slice(0, cut)
        partial = compiled.evaluate(partial_bars, instrument)
        last = cut - 1
        for field_name in ("long_entry", "short_entry", "long_exit", "short_exit", "invalidation"):
            with_future = bool(getattr(full, field_name)[last])
            without_future = bool(getattr(partial, field_name)[last])
            if with_future != without_future:
                report.divergences.append(
                    {
                        "index": last,
                        "time": bars.df["ts"].iloc[last].isoformat(),
                        "field": field_name,
                        "with_future": with_future,
                        "without_future": without_future,
                    }
                )
    if report.divergences:
        report.verdict = "lookahead detected"
    return report


def check_fills(result, bars: Bars) -> LookaheadReport:
    """A fill price outside the bar it happened on is information from the future."""
    report = LookaheadReport()
    tolerance = 1e-6
    for trade in result.trades:
        for label, index, price in (
            ("entry", trade.entry_index, trade.entry_price),
            ("exit", trade.exit_index, trade.exit_price),
        ):
            if index is None or price is None or index >= len(bars):
                continue
            high, low = bars.high[index], bars.low[index]
            # A slipped or gapped fill may legitimately exceed the bar range;
            # only an *improvement* beyond the range is impossible.
            improved_long = trade.side == "long" and label == "entry" and price < low - tolerance
            improved_short = trade.side == "short" and label == "entry" and price > high + tolerance
            improved_exit_long = trade.side == "long" and label == "exit" and price > high + tolerance
            improved_exit_short = trade.side == "short" and label == "exit" and price < low - tolerance
            if improved_long or improved_short or improved_exit_long or improved_exit_short:
                report.impossible_fills.append(
                    {
                        "trade": trade.id,
                        "field": label,
                        "price": float(price),
                        "index": int(index),
                        "high": float(high),
                        "low": float(low),
                    }
                )
    if report.impossible_fills:
        report.verdict = "impossible fills"
    return report


def full_check(compiled: CompiledStrategy, bars: Bars, result=None, instrument=None, **kw) -> LookaheadReport:
    report = detect(compiled, bars, instrument=instrument, **kw)
    if result is not None:
        fills = check_fills(result, bars)
        report.impossible_fills = fills.impossible_fills
        if fills.impossible_fills and report.clean:
            report.verdict = fills.verdict
    return report
