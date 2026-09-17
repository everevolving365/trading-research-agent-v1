"""Drift monitoring, execution quality scorecard and shadow mode (58, 59, 60).

Every live fill is compared against the distribution the backtest predicted.
Outside the band raises a flag; enough flags demote the strategy automatically
(the autonomy ladder does not ask).

The scorecard answers a different question: not "is the strategy still working"
but "am I getting the fills the backtest assumed" -- expected versus actual,
per trade, aggregated by strategy, session and instrument.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np


@dataclass
class Baseline:
    """What the backtest predicted. Computed once, from the pinned report."""

    spec_id: str
    symbol: str
    mean_pnl: float
    std_pnl: float
    win_rate: float
    mean_bars_held: float
    mean_slippage_ticks: float
    n_trades: int

    @classmethod
    def from_report(cls, report) -> "Baseline":
        trades = [t for t in report.base.trades if t.closed]
        pnls = np.array([t.net_pnl for t in trades]) if trades else np.array([0.0])
        slips = np.array(
            [t.slippage_cost / max(abs(t.size), 1e-9) for t in trades]
        ) if trades else np.array([0.0])
        return cls(
            spec_id=report.spec.id,
            symbol=report.base.symbol,
            mean_pnl=float(pnls.mean()),
            std_pnl=float(pnls.std()) or 1.0,
            win_rate=report.base.metrics.win_rate,
            mean_bars_held=report.base.metrics.avg_bars_held,
            mean_slippage_ticks=float(slips.mean()),
            n_trades=len(trades),
        )

    def to_dict(self) -> dict:
        return self.__dict__


@dataclass
class DriftFlag:
    ts: str
    metric: str
    observed: float
    expected: float
    sigma: float
    message: str

    def to_dict(self) -> dict:
        return self.__dict__


@dataclass
class ExecutionScore:
    """Expected versus actual fill, per trade and aggregated."""

    trades: int = 0
    total_slippage_cost: float = 0.0
    expected_slippage_cost: float = 0.0
    worst_fill_ticks: float = 0.0
    by_session: dict[str, dict] = field(default_factory=dict)
    by_instrument: dict[str, dict] = field(default_factory=dict)

    @property
    def excess_cost(self) -> float:
        return self.total_slippage_cost - self.expected_slippage_cost

    @property
    def grade(self) -> str:
        if self.expected_slippage_cost <= 0:
            return "n/a"
        ratio = self.total_slippage_cost / self.expected_slippage_cost
        if ratio <= 1.1:
            return "A -- fills match the backtest's assumptions"
        if ratio <= 1.5:
            return "B -- fills are worse than assumed but the edge survives"
        if ratio <= 2.5:
            return "C -- execution is eating a material share of the edge"
        return "F -- you are not getting the fills the backtest assumed"

    def to_dict(self) -> dict:
        return {
            "trades": self.trades,
            "total_slippage_cost": round(self.total_slippage_cost, 4),
            "expected_slippage_cost": round(self.expected_slippage_cost, 4),
            "excess_cost": round(self.excess_cost, 4),
            "worst_fill_ticks": round(self.worst_fill_ticks, 3),
            "grade": self.grade,
            "by_session": self.by_session,
            "by_instrument": self.by_instrument,
        }

    def summary(self) -> str:
        return (
            f"[execution] {self.trades} live fill(s): slippage cost {self.total_slippage_cost:,.2f} "
            f"against {self.expected_slippage_cost:,.2f} expected "
            f"({self.excess_cost:+,.2f}). Grade {self.grade}"
        )


class DriftMonitor:
    def __init__(self, baseline: Baseline, sigma_threshold: float = 3.0, window: int = 20):
        self.baseline = baseline
        self.sigma_threshold = sigma_threshold
        self.window = window
        self.live_pnls: list[float] = []
        self.flags: list[DriftFlag] = []
        self.score = ExecutionScore()

    # ------------------------------------------------------------ observing
    def observe_trade(
        self,
        pnl: float,
        *,
        session: str = "",
        symbol: str = "",
        actual_slippage_ticks: float = 0.0,
        expected_slippage_ticks: float | None = None,
        tick_value: float = 1.0,
        size: float = 1.0,
    ) -> list[DriftFlag]:
        self.live_pnls.append(pnl)
        expected_ticks = (
            self.baseline.mean_slippage_ticks if expected_slippage_ticks is None else expected_slippage_ticks
        )
        self.score.trades += 1
        self.score.total_slippage_cost += actual_slippage_ticks * tick_value * size
        self.score.expected_slippage_cost += expected_ticks * tick_value * size
        self.score.worst_fill_ticks = max(self.score.worst_fill_ticks, actual_slippage_ticks)
        for bucket, key in ((self.score.by_session, session), (self.score.by_instrument, symbol)):
            if not key:
                continue
            row = bucket.setdefault(key, {"trades": 0, "slippage_cost": 0.0, "pnl": 0.0})
            row["trades"] += 1
            row["slippage_cost"] = round(row["slippage_cost"] + actual_slippage_ticks * tick_value * size, 4)
            row["pnl"] = round(row["pnl"] + pnl, 2)

        return self.check()

    def check(self) -> list[DriftFlag]:
        """Is the live distribution still the one the backtest predicted?"""
        new_flags: list[DriftFlag] = []
        if len(self.live_pnls) < max(5, self.window // 4):
            return new_flags
        recent = np.array(self.live_pnls[-self.window :])
        n = len(recent)
        standard_error = self.baseline.std_pnl / np.sqrt(n)
        sigma = abs(float(recent.mean()) - self.baseline.mean_pnl) / max(standard_error, 1e-9)
        if sigma >= self.sigma_threshold:
            new_flags.append(
                DriftFlag(
                    ts=datetime.now(timezone.utc).isoformat(),
                    metric="mean_pnl",
                    observed=float(recent.mean()),
                    expected=self.baseline.mean_pnl,
                    sigma=round(sigma, 2),
                    message=(
                        f"the last {n} live trades average {recent.mean():,.2f} against a backtest "
                        f"expectation of {self.baseline.mean_pnl:,.2f} -- {sigma:.1f} sigma out"
                    ),
                )
            )
        live_wr = float((recent > 0).mean())
        wr_se = np.sqrt(max(self.baseline.win_rate * (1 - self.baseline.win_rate), 1e-9) / n)
        wr_sigma = abs(live_wr - self.baseline.win_rate) / max(wr_se, 1e-9)
        if wr_sigma >= self.sigma_threshold:
            new_flags.append(
                DriftFlag(
                    ts=datetime.now(timezone.utc).isoformat(),
                    metric="win_rate",
                    observed=live_wr,
                    expected=self.baseline.win_rate,
                    sigma=round(float(wr_sigma), 2),
                    message=(
                        f"live win rate {live_wr:.0%} against {self.baseline.win_rate:.0%} expected "
                        f"-- {wr_sigma:.1f} sigma out"
                    ),
                )
            )
        self.flags.extend(new_flags)
        return new_flags

    @property
    def max_sigma(self) -> float:
        return max((f.sigma for f in self.flags), default=0.0)

    def apply_to(self, ladder) -> None:
        """Demotion on drift is automatic (ability 57)."""
        if not self.flags:
            return
        ladder.evidence.drift_flags = len(self.flags)
        worst = max(self.flags, key=lambda f: f.sigma)
        if worst.sigma >= self.sigma_threshold:
            ladder.demote(f"drift: {worst.message}")

    def summary(self) -> str:
        if not self.flags:
            return (
                f"[drift] {len(self.live_pnls)} live trade(s) within the band predicted by "
                f"{self.baseline.n_trades} backtested trades."
            )
        lines = [f"[drift] {len(self.flags)} flag(s):"]
        for f in self.flags[-5:]:
            lines.append(f"    {f.metric}: {f.message}")
        return "\n".join(lines)


class ShadowBook:
    """Shadow mode: every live strategy also runs on paper, so "what would it
    have done" is always answerable (ability 60)."""

    def __init__(self, strategy_id: str):
        self.strategy_id = strategy_id
        self.paper_trades: list[dict] = []
        self.live_trades: list[dict] = []

    def record_paper(self, **trade) -> None:
        self.paper_trades.append({"ts": datetime.now(timezone.utc).isoformat(), **trade})

    def record_live(self, **trade) -> None:
        self.live_trades.append({"ts": datetime.now(timezone.utc).isoformat(), **trade})

    def divergence(self) -> dict:
        paper_pnl = sum(t.get("pnl", 0.0) for t in self.paper_trades)
        live_pnl = sum(t.get("pnl", 0.0) for t in self.live_trades)
        return {
            "strategy_id": self.strategy_id,
            "paper_trades": len(self.paper_trades),
            "live_trades": len(self.live_trades),
            "paper_pnl": round(paper_pnl, 2),
            "live_pnl": round(live_pnl, 2),
            "gap": round(live_pnl - paper_pnl, 2),
            "note": (
                "The gap is execution: the paper book took every signal at the modelled price."
                if len(self.paper_trades) == len(self.live_trades)
                else "The books took a different NUMBER of trades -- something blocked a live entry."
            ),
        }

    def summary(self) -> str:
        d = self.divergence()
        return (
            f"[shadow] live {d['live_trades']} trades {d['live_pnl']:,.2f} vs paper "
            f"{d['paper_trades']} trades {d['paper_pnl']:,.2f} (gap {d['gap']:+,.2f}). {d['note']}"
        )
