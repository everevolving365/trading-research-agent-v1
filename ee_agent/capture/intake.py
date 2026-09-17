"""Optional intake paths (ability 15) and natural language editing (ability 14).

Two alternatives to describing the strategy out loud:

* upload marked-up screenshots of past trades, and the agent infers the rules;
* point it at broker fill history, and it reconstructs what the client actually
  trades.

Both produce the same object as the spoken path -- a strategy spec with an
ambiguity ledger -- so they inherit every guarantee in the system.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ee_agent.spec.model import Assumption, Distance, Sizing, StrategySpec


# ============================================================ fill history
@dataclass
class ReconstructedBehaviour:
    """What the client ACTUALLY does, measured from their own fills."""

    n_fills: int = 0
    n_round_turns: int = 0
    symbols: list[str] = field(default_factory=list)
    sessions: dict[str, int] = field(default_factory=dict)
    median_hold_minutes: float = 0.0
    median_size: float = 1.0
    median_stop_ticks: float | None = None
    median_target_ticks: float | None = None
    win_rate: float = 0.0
    trades_per_day: float = 0.0
    first_entry_minute: int | None = None
    last_exit_minute: int | None = None
    long_share: float = 0.5
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"[intake] reconstructed from {self.n_fills:,} fills -> {self.n_round_turns:,} round turns "
            f"on {', '.join(self.symbols[:6])}"
        ]
        if self.first_entry_minute is not None:
            lines.append(
                f"    you enter between {_hhmm(self.first_entry_minute)} and "
                f"{_hhmm(self.last_exit_minute or self.first_entry_minute)}, "
                f"{self.trades_per_day:.1f} trades a day, median hold {self.median_hold_minutes:.0f} minutes"
            )
        lines.append(
            f"    median size {self.median_size:g}, {self.long_share:.0%} long, win rate {self.win_rate:.0%}"
        )
        if self.median_stop_ticks:
            lines.append(
                f"    your losers cluster around {self.median_stop_ticks:,.0f} ticks and your winners "
                f"around {self.median_target_ticks or 0:,.0f} ticks -- that looks like your stop and target"
            )
        lines += [f"    {n}" for n in self.notes]
        lines.append(
            "    This is what your history SHOWS, not what you intend. Nothing here becomes a rule "
            "until you confirm it."
        )
        return "\n".join(lines)


def reconstruct_from_fills(fills: pd.DataFrame, tick_size: float = 0.25) -> ReconstructedBehaviour:
    """Pair fills into round turns and measure the behaviour they imply."""
    out = ReconstructedBehaviour()
    if fills is None or len(fills) == 0:
        return out
    df = fills.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts", "price", "qty", "side"]).sort_values("ts").reset_index(drop=True)
    out.n_fills = len(df)
    out.symbols = sorted(df["symbol"].astype(str).unique().tolist()) if "symbol" in df else []

    round_turns: list[dict] = []
    for symbol, group in df.groupby(df.get("symbol", "ALL")):
        position = 0.0
        entry_price = 0.0
        entry_time = None
        entry_side = ""
        for _, row in group.iterrows():
            signed = float(row["qty"]) * (1 if str(row["side"]).lower().startswith("b") else -1)
            if position == 0:
                position, entry_price, entry_time = signed, float(row["price"]), row["ts"]
                entry_side = "long" if signed > 0 else "short"
            elif (position > 0) != (signed > 0):
                closed = min(abs(position), abs(signed))
                direction = 1 if position > 0 else -1
                points = (float(row["price"]) - entry_price) * direction
                round_turns.append(
                    {
                        "symbol": symbol,
                        "side": entry_side,
                        "entry_time": entry_time,
                        "exit_time": row["ts"],
                        "hold_minutes": (row["ts"] - entry_time).total_seconds() / 60.0,
                        "points": points,
                        "ticks": points / tick_size if tick_size else points,
                        "size": closed,
                    }
                )
                position += signed
                if position != 0:
                    entry_price, entry_time = float(row["price"]), row["ts"]
                    entry_side = "long" if position > 0 else "short"
            else:
                total = abs(position) + abs(signed)
                entry_price = (entry_price * abs(position) + float(row["price"]) * abs(signed)) / total
                position += signed

    out.n_round_turns = len(round_turns)
    if not round_turns:
        out.notes.append("no completed round turns found: every fill opened a position that never closed")
        return out

    rt = pd.DataFrame(round_turns)
    out.median_hold_minutes = float(rt["hold_minutes"].median())
    out.median_size = float(rt["size"].median())
    out.win_rate = float((rt["ticks"] > 0).mean())
    out.long_share = float((rt["side"] == "long").mean())
    losers, winners = rt[rt["ticks"] < 0], rt[rt["ticks"] > 0]
    if len(losers) >= 3:
        out.median_stop_ticks = float(abs(losers["ticks"]).median())
    if len(winners) >= 3:
        out.median_target_ticks = float(winners["ticks"].median())
    entry_minutes = rt["entry_time"].dt.hour * 60 + rt["entry_time"].dt.minute
    exit_minutes = rt["exit_time"].dt.hour * 60 + rt["exit_time"].dt.minute
    out.first_entry_minute = int(entry_minutes.quantile(0.05))
    out.last_exit_minute = int(exit_minutes.quantile(0.95))
    days = rt["entry_time"].dt.date.nunique()
    out.trades_per_day = len(rt) / max(days, 1)
    if out.median_stop_ticks and out.median_target_ticks:
        ratio = out.median_target_ticks / out.median_stop_ticks
        out.notes.append(f"your realised reward-to-risk is about {ratio:.2f} to 1")
    if len(losers) and abs(losers["ticks"]).std() / max(abs(losers["ticks"]).median(), 1) > 0.8:
        out.notes.append(
            "your losses vary a lot in size, which usually means the stop is discretionary rather "
            "than a fixed distance"
        )
    return out


def spec_from_fills(
    behaviour: ReconstructedBehaviour, strategy_id: str = "reconstructed-v1", timezone: str = "America/Chicago"
) -> StrategySpec:
    """Build a spec skeleton from observed behaviour.

    Everything inferred is written as an UNAPPROVED assumption. The agent has not
    decided anything: it has measured, and it is asking.
    """
    from ee_agent.spec.model import Costs, Execution, Filters, Risk, Signals, TimeWindow, Universe

    spec = StrategySpec(
        id=strategy_id,
        name="Reconstructed from your fill history",
        universe=Universe(instruments=behaviour.symbols[:4] or ["MNQ"], timezone=timezone),
        filters=Filters(
            time_windows=[
                TimeWindow(
                    start=_hhmm(behaviour.first_entry_minute or 510),
                    end=_hhmm(behaviour.last_exit_minute or 900),
                    tz=timezone,
                )
            ],
            max_trades_per_day=max(1, int(round(behaviour.trades_per_day))),
        ),
        risk=Risk(
            stop=Distance("ticks", behaviour.median_stop_ticks) if behaviour.median_stop_ticks else None,
            target=Distance("ticks", behaviour.median_target_ticks) if behaviour.median_target_ticks else None,
            size=Sizing("fixed_contracts", behaviour.median_size or 1),
        ),
        execution=Execution(),
        costs=Costs(commission_per_side=0.0, slippage_ticks=1.0, spread_ticks=1.0),
        signals=Signals(),
    )
    spec.assumptions = [
        Assumption(
            id="fills:stop",
            question="Is this your stop distance, or just where your losses happened to land?",
            resolution=f"{behaviour.median_stop_ticks or 'unknown'} ticks, the median of your losing trades",
            approved_by="pending",
            sensitivity={"if_wrong": "every loss in the backtest is the wrong size"},
        ),
        Assumption(
            id="fills:target",
            question="Is this your profit target?",
            resolution=f"{behaviour.median_target_ticks or 'unknown'} ticks, the median of your winners",
            approved_by="pending",
            sensitivity={"if_wrong": "every win in the backtest is the wrong size"},
        ),
        Assumption(
            id="fills:entry_rule",
            question="Your fills show WHEN you traded but not WHY. What is the actual entry trigger?",
            resolution="unknown -- no entry rule can be inferred from fills alone",
            approved_by="pending",
            sensitivity={"if_wrong": "there is no strategy to compile until this is answered"},
        ),
    ]
    return spec


# ============================================================== screenshots
@dataclass
class ScreenshotHint:
    path: str
    symbol: str | None = None
    timeframe: str | None = None
    timestamps: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    note: str = ""


def read_screenshots(paths: list[str | Path], model=None) -> list[ScreenshotHint]:
    """Marked-up screenshots of past trades.

    Without a vision model this extracts what is mechanically available -- the
    filename, any timestamp in it, and image metadata -- and says plainly that
    the drawing on the chart needs a model to read. It never guesses at the
    contents of an image it cannot see.
    """
    hints: list[ScreenshotHint] = []
    for raw in paths:
        path = Path(raw)
        hint = ScreenshotHint(path=str(path))
        stem = path.stem
        symbol_match = re.search(r"\b(MNQ|NQ|ES|MES|CL|SPY|QQQ|BTCUSDT|ETHUSDT|EURUSD)\b", stem, re.I)
        if symbol_match:
            hint.symbol = symbol_match.group(1).upper()
        tf_match = re.search(r"\b(\d+)\s*(m|min|h|hour)\b", stem, re.I)
        if tf_match:
            hint.timeframe = f"{tf_match.group(1)}{'m' if tf_match.group(2).lower().startswith('m') else 'h'}"
        for ts in re.findall(r"\d{4}[-_]\d{2}[-_]\d{2}", stem):
            hint.timestamps.append(ts.replace("_", "-"))
        if model is not None:  # pragma: no cover - needs a vision model
            try:
                hint.annotations = model.describe_chart(str(path))
                hint.note = "annotations read by the vision model"
            except Exception as exc:
                hint.note = f"vision model failed: {exc}"
        else:
            hint.note = (
                "I can see the file but not what is drawn on it. Reading the markup on a chart needs "
                "a vision model -- add a model key, or just tell me what the arrows mean."
            )
        hints.append(hint)
    return hints


# ======================================================= natural language edit
@dataclass
class EditResult:
    before: StrategySpec
    after: StrategySpec
    applied: bool
    change: str = ""
    reason: str = ""

    def diff_summary(self) -> str:
        from ee_agent.spec.diff import diff

        return diff(self.before, self.after).summary()


EDIT_PATTERNS: list[tuple[str, str]] = [
    (r"\b(?:make|set|move|change)\s+the\s+stop\s+(?:to\s+)?(?P<value>[\d.]+)\s*(?P<unit>ticks?|points?|atr|percent|%)", "stop"),
    (r"\b(?:make|set|move|change)\s+the\s+target\s+(?:to\s+)?(?P<value>[\d.]+)\s*(?P<unit>ticks?|points?|atr|percent|%)", "target"),
    (r"\bstop\s+(?:to|at)\s+(?P<value>[\d.]+)\s*(?P<unit>ticks?|points?|atr|percent|%)", "stop"),
    (r"\btarget\s+(?:to|at)\s+(?P<value>[\d.]+)\s*(?P<unit>ticks?|points?|atr|percent|%)", "target"),
    (r"\b(?:max|maximum|no more than|only)\s+(?P<value>\d+)\s+trades?\s+(?:a|per)\s+day", "max_trades"),
    (r"\bstop\s+trading\s+at\s+(?P<time>\d{1,2}[:.]?\d{0,2}\s*(?:am|pm)?)", "window_end"),
    (r"\bstart\s+(?:trading\s+)?at\s+(?P<time>\d{1,2}[:.]?\d{0,2}\s*(?:am|pm)?)", "window_start"),
    (r"\b(?:only|just)\s+(?:trade\s+)?(?P<side>long|short)s?\b", "side_only"),
    (r"\b(?:don't|do not|stop)\s+(?:take|trading)\s+(?P<side>long|short)s?\b", "side_drop"),
    (r"\b(?:size|trade)\s+(?P<value>\d+)\s+contracts?\b", "size"),
]


def edit(spec: StrategySpec, sentence: str) -> EditResult:
    """One spoken sentence changes the strategy (ability 14).

    Returns before and after so the caller can regenerate everything and show
    the results side by side. An unrecognised sentence changes nothing and says
    so -- it never half-applies.
    """
    from ee_agent.spec.model import TimeWindow

    after = StrategySpec.from_dict(spec.to_dict())
    lowered = sentence.lower().strip()

    for pattern, kind in EDIT_PATTERNS:
        m = re.search(pattern, lowered)
        if not m:
            continue
        if kind in ("stop", "target"):
            unit = m.group("unit").rstrip("s")
            unit = {"tick": "ticks", "point": "points", "%": "percent", "percent": "percent", "atr": "atr"}.get(
                unit, "ticks"
            )
            distance = Distance(type=unit, value=float(m.group("value")))
            setattr(after.risk, kind, distance)
            return EditResult(spec, after, True, f"{kind} -> {distance.value} {distance.type}")
        if kind == "max_trades":
            after.filters.max_trades_per_day = int(m.group("value"))
            return EditResult(spec, after, True, f"max trades per day -> {m.group('value')}")
        if kind in ("window_start", "window_end"):
            from ee_agent.capture.parser import _minutes_to_hhmm

            raw = m.group("time").strip()
            ampm = "pm" if "pm" in raw else ("am" if "am" in raw else None)
            digits = re.sub(r"[^\d:.]", "", raw).replace(".", ":")
            hour, _, minute = digits.partition(":")
            hhmm = _minutes_to_hhmm(int(hour), int(minute or 0), ampm)
            if not after.filters.time_windows:
                after.filters.time_windows = [TimeWindow(start="09:30", end="15:00", tz=after.universe.timezone)]
            window = after.filters.time_windows[0]
            if kind == "window_start":
                after.filters.time_windows[0] = TimeWindow(hhmm, window.end, window.tz)
            else:
                after.filters.time_windows[0] = TimeWindow(window.start, hhmm, window.tz)
            return EditResult(spec, after, True, f"{kind.replace('_', ' ')} -> {hhmm}")
        if kind == "side_only":
            keep = m.group("side")
            after.signals.entry = [r for r in after.signals.entry if (r.side or r.id) == keep]
            return EditResult(spec, after, True, f"{keep} side only")
        if kind == "side_drop":
            drop = m.group("side")
            after.signals.entry = [r for r in after.signals.entry if (r.side or r.id) != drop]
            return EditResult(spec, after, True, f"dropped the {drop} side")
        if kind == "size":
            after.risk.size = Sizing("fixed_contracts", float(m.group("value")))
            return EditResult(spec, after, True, f"size -> {m.group('value')} contracts")

    return EditResult(
        spec,
        spec,
        False,
        reason=(
            f"I did not understand {sentence!r} as a change to the strategy, so I have changed nothing. "
            "Try something like 'make the stop 1.5 ATR' or 'only trade shorts' or 'stop trading at 2pm'."
        ),
    )


def before_and_after(spec: StrategySpec, sentence: str, bars) -> str:
    """Ability 14's second half: everything regenerates and the results are
    shown together."""
    from ee_agent.engine.backtester import Backtester

    result = edit(spec, sentence)
    if not result.applied:
        return result.reason
    before = Backtester(result.before).run(bars, window_label="before")
    after = Backtester(result.after).run(bars, window_label="after")
    return "\n".join(
        [
            f'[edit] "{sentence}"  ->  {result.change}',
            result.diff_summary(),
            "",
            f"    {'':<18}{'BEFORE':>14}{'AFTER':>14}",
            f"    {'trades':<18}{before.metrics.n_trades:>14,}{after.metrics.n_trades:>14,}",
            f"    {'net P&L':<18}{before.metrics.net_pnl:>14,.2f}{after.metrics.net_pnl:>14,.2f}",
            f"    {'max drawdown':<18}{before.metrics.max_drawdown:>14,.2f}{after.metrics.max_drawdown:>14,.2f}",
            f"    {'win rate':<18}{before.metrics.win_rate:>13.1%}{after.metrics.win_rate:>14.1%}",
            f"    {'profit factor':<18}{before.metrics.profit_factor:>14.2f}{after.metrics.profit_factor:>14.2f}",
            "",
            "    Neither version is a defensible number on its own -- run the truth engine on the one "
            "you keep.",
        ]
    )


def _hhmm(minutes: int | None) -> str:
    if minutes is None:
        return "00:00"
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"
