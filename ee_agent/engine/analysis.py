"""Walk-forward, Monte Carlo, regime decomposition, synthetic markets and
multiple-comparison correction (abilities 38-41, 43, 44).

Law One: no result from a single window, and every number carries its band.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from ee_agent.data.bars import Bars
from ee_agent.engine.metrics import Metrics, max_drawdown


# ============================================================== walk-forward
@dataclass
class Window:
    label: str
    train_start: int
    train_end: int
    test_start: int
    test_end: int

    def describe(self, bars: Bars) -> str:
        return (
            f"{self.label}: train {bars.df['ts'].iloc[self.train_start].date()}"
            f"->{bars.df['ts'].iloc[self.train_end - 1].date()}  "
            f"test {bars.df['ts'].iloc[self.test_start].date()}"
            f"->{bars.df['ts'].iloc[self.test_end - 1].date()}"
        )


def walk_forward_windows(
    n_bars: int, n_windows: int = 5, train_fraction: float = 0.6, anchored: bool = False
) -> list[Window]:
    """Anchored and rolling, both supported, rolling by default.

    Anchored keeps the training start fixed and grows the window; rolling slides
    it. Anchored answers "does it still work as I learn more"; rolling answers
    "does it work in a regime I did not train on".
    """
    windows: list[Window] = []
    if n_bars < n_windows * 20:
        return windows
    block = n_bars // (n_windows + 1)
    train_len = max(int(block * (train_fraction / (1 - train_fraction))), block)
    for k in range(n_windows):
        test_start = block * (k + 1)
        test_end = min(test_start + block, n_bars)
        train_start = 0 if anchored else max(0, test_start - train_len)
        if test_end - test_start < 20 or test_start - train_start < 20:
            continue
        windows.append(
            Window(
                label=f"{'anchored' if anchored else 'rolling'}-{k + 1}",
                train_start=train_start,
                train_end=test_start,
                test_start=test_start,
                test_end=test_end,
            )
        )
    return windows


@dataclass
class WalkForwardResult:
    windows: list[dict] = field(default_factory=list)
    in_sample: Metrics | None = None
    out_of_sample: Metrics | None = None
    degradation: dict = field(default_factory=dict)
    mode: str = "rolling"

    @property
    def oos_positive_windows(self) -> int:
        return sum(1 for w in self.windows if w["test"]["net_pnl"] > 0)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "n_windows": len(self.windows),
            "oos_positive_windows": self.oos_positive_windows,
            "in_sample": self.in_sample.to_dict() if self.in_sample else None,
            "out_of_sample": self.out_of_sample.to_dict() if self.out_of_sample else None,
            "degradation": self.degradation,
            "windows": self.windows,
        }

    def summary(self) -> str:
        if not self.windows:
            return "[walk-forward] not enough data to split into windows"
        lines = [
            f"[walk-forward] {self.mode}, {len(self.windows)} window(s); "
            f"{self.oos_positive_windows} of {len(self.windows)} profitable out of sample"
        ]
        if self.in_sample and self.out_of_sample:
            lines.append(
                f"    in-sample  net {self.in_sample.net_pnl:>12,.2f}  PF {self.in_sample.profit_factor:5.2f}  "
                f"win {self.in_sample.win_rate:5.1%}  trades {self.in_sample.n_trades}"
            )
            lines.append(
                f"    out-sample net {self.out_of_sample.net_pnl:>12,.2f}  PF {self.out_of_sample.profit_factor:5.2f}  "
                f"win {self.out_of_sample.win_rate:5.1%}  trades {self.out_of_sample.n_trades}"
            )
            deg = self.degradation.get("profit_factor_ratio")
            if deg is not None:
                lines.append(
                    f"    out-of-sample profit factor is {deg:.0%} of in-sample "
                    f"({'holds up' if deg >= 0.7 else 'DEGRADES'})"
                )
        return "\n".join(lines)


def run_walk_forward(
    run_window: Callable[[int, int, str], object],
    n_bars: int,
    n_windows: int = 5,
    anchored: bool = False,
) -> WalkForwardResult:
    """``run_window(start, end, label)`` returns a BacktestResult."""
    windows = walk_forward_windows(n_bars, n_windows=n_windows, anchored=anchored)
    out = WalkForwardResult(mode="anchored" if anchored else "rolling")
    is_trades, oos_trades = [], []
    is_equity, oos_equity = [], []
    for w in windows:
        train = run_window(w.train_start, w.train_end, f"{w.label}-train")
        test = run_window(w.test_start, w.test_end, f"{w.label}-test")
        out.windows.append(
            {
                "label": w.label,
                "train": train.metrics.to_dict(),
                "test": test.metrics.to_dict(),
                "train_start": train.start,
                "test_start": test.start,
            }
        )
        is_trades.extend(train.trades)
        oos_trades.extend(test.trades)
        is_equity.append(train.equity)
        oos_equity.append(test.equity)

    if windows:
        out.in_sample = _aggregate(is_trades, is_equity)
        out.out_of_sample = _aggregate(oos_trades, oos_equity)
        pf_is = out.in_sample.profit_factor or 0.0
        pf_oos = out.out_of_sample.profit_factor or 0.0
        out.degradation = {
            "profit_factor_ratio": round(pf_oos / pf_is, 4) if pf_is > 0 else None,
            "expectancy_ratio": (
                round(out.out_of_sample.expectancy / out.in_sample.expectancy, 4)
                if out.in_sample.expectancy
                else None
            ),
        }
    return out


def _aggregate(trades, equities) -> Metrics:
    m = Metrics()
    closed = [t for t in trades if t.closed]
    m.n_trades = len(closed)
    if not closed:
        return m
    pnls = np.array([t.net_pnl for t in closed])
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    m.net_pnl = float(pnls.sum())
    m.costs = float(sum(t.commission for t in closed))
    m.gross_pnl = float(sum(t.gross_pnl for t in closed))
    m.win_rate = len(wins) / len(pnls)
    m.avg_win = float(wins.mean()) if len(wins) else 0.0
    m.avg_loss = float(losses.mean()) if len(losses) else 0.0
    gl = abs(float(losses.sum()))
    m.profit_factor = float(wins.sum()) / gl if gl else (np.inf if len(wins) else 0.0)
    m.expectancy = float(pnls.mean())
    m.avg_bars_held = float(np.mean([t.bars_held for t in closed]))
    curve = np.cumsum(pnls)
    m.max_drawdown, m.max_drawdown_pct = max_drawdown(curve)
    m.return_on_max_dd = m.net_pnl / m.max_drawdown if m.max_drawdown else 0.0
    return m


# =============================================================== Monte Carlo
@dataclass
class MonteCarloResult:
    n_paths: int
    method: str
    drawdown_p50: float = 0.0
    drawdown_p95: float = 0.0
    drawdown_p99: float = 0.0
    drawdown_worst: float = 0.0
    final_p05: float = 0.0
    final_p50: float = 0.0
    final_p95: float = 0.0
    prob_profitable: float = 0.0
    prob_ruin: float = 0.0
    ruin_threshold: float = 0.0

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}

    def band(self) -> str:
        return (
            f"[monte carlo] {self.n_paths:,} {self.method} paths: drawdown median "
            f"{self.drawdown_p50:,.0f}, 95th {self.drawdown_p95:,.0f}, worst {self.drawdown_worst:,.0f}; "
            f"final P&L 5-95% band [{self.final_p05:,.0f}, {self.final_p95:,.0f}]; "
            f"{self.prob_profitable:.0%} of paths profitable"
        )


def monte_carlo(
    trade_pnls: list[float] | np.ndarray,
    n_paths: int = 2000,
    method: str = "resample",
    seed: int = 23,
    ruin_threshold: float = 2000.0,
    block: int = 5,
) -> MonteCarloResult:
    """Drawdown distribution on every result (ability 40).

    ``resample`` shuffles trades with replacement -- the standard bootstrap.
    ``block`` resamples in blocks, preserving streaks, which matters because
    consecutive losses are what actually breach a prop firm's daily limit.
    """
    pnls = np.asarray(list(trade_pnls), dtype=float)
    out = MonteCarloResult(n_paths=n_paths, method=method, ruin_threshold=ruin_threshold)
    if len(pnls) < 2:
        return out
    rng = np.random.default_rng(seed)
    dds, finals = np.zeros(n_paths), np.zeros(n_paths)
    ruined = 0
    for p in range(n_paths):
        if method == "block":
            n_blocks = int(np.ceil(len(pnls) / block))
            starts = rng.integers(0, max(len(pnls) - block, 1), size=n_blocks)
            path = np.concatenate([pnls[s : s + block] for s in starts])[: len(pnls)]
        else:
            path = rng.choice(pnls, size=len(pnls), replace=True)
        curve = np.cumsum(path)
        dd, _ = max_drawdown(curve)
        dds[p] = dd
        finals[p] = curve[-1]
        if dd >= ruin_threshold:
            ruined += 1
    out.drawdown_p50 = float(np.percentile(dds, 50))
    out.drawdown_p95 = float(np.percentile(dds, 95))
    out.drawdown_p99 = float(np.percentile(dds, 99))
    out.drawdown_worst = float(dds.max())
    out.final_p05 = float(np.percentile(finals, 5))
    out.final_p50 = float(np.percentile(finals, 50))
    out.final_p95 = float(np.percentile(finals, 95))
    out.prob_profitable = float((finals > 0).mean())
    out.prob_ruin = ruined / n_paths
    return out


# ========================================================= regime decomposition
REGIME_DIMENSIONS = ("trend_vs_range", "volatility", "session", "day_of_week", "news_day")


@dataclass
class RegimeBreakdown:
    by_dimension: dict[str, dict[str, dict]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.by_dimension

    def summary(self) -> str:
        lines = ["[regimes] where the result actually comes from:"]
        for dim, buckets in self.by_dimension.items():
            parts = []
            for name, stats in buckets.items():
                parts.append(f"{name}: {stats['n']} trades, net {stats['net_pnl']:,.0f}")
            lines.append(f"    {dim:<15} " + " | ".join(parts))
        return "\n".join(lines)

    def concentration(self) -> tuple[str, float]:
        """The single bucket carrying the most of the profit. If one bucket is
        the whole result, the strategy is a regime bet."""
        worst_dim, worst_share = "", 0.0
        for dim, buckets in self.by_dimension.items():
            total = sum(max(b["net_pnl"], 0) for b in buckets.values())
            if total <= 0:
                continue
            for name, stats in buckets.items():
                share = max(stats["net_pnl"], 0) / total
                if share > worst_share:
                    worst_dim, worst_share = f"{dim}:{name}", share
        return worst_dim, worst_share


def decompose_regimes(trades, bars: Bars, calendar_days: set[str] | None = None) -> RegimeBreakdown:
    from ee_agent.spec.primitives import _sma, atr_series

    out = RegimeBreakdown()
    closed = [t for t in trades if t.closed]
    if not closed:
        return out

    atr = atr_series(bars, 14)
    atr_base = _sma(np.nan_to_num(atr), 100)
    close = bars.close
    sma_fast, sma_slow = _sma(close, 20), _sma(close, 100)

    def bucket(fn) -> dict[str, dict]:
        groups: dict[str, list] = {}
        for t in closed:
            key = fn(t)
            groups.setdefault(key, []).append(t)
        return {
            k: {
                "n": len(v),
                "net_pnl": round(float(sum(x.net_pnl for x in v)), 2),
                "win_rate": round(sum(1 for x in v if x.net_pnl > 0) / len(v), 4),
                "expectancy": round(float(np.mean([x.net_pnl for x in v])), 2),
            }
            for k, v in sorted(groups.items())
        }

    def trend_or_range(t) -> str:
        i = min(t.entry_index, len(bars) - 1)
        if np.isnan(sma_slow[i]) or np.isnan(sma_fast[i]):
            return "unknown"
        return "trend" if abs(sma_fast[i] - sma_slow[i]) / max(atr[i], 1e-9) > 1.0 else "range"

    def volatility(t) -> str:
        i = min(t.entry_index, len(bars) - 1)
        if np.isnan(atr_base[i]) or atr_base[i] <= 0:
            return "unknown"
        return "high_vol" if atr[i] / atr_base[i] > 1.1 else "low_vol"

    def session(t) -> str:
        minute = bars.minute_of_day[min(t.entry_index, len(bars) - 1)]
        if minute < 10 * 60:
            return "open"
        if minute < 13 * 60:
            return "midday"
        return "close"

    def weekday(t) -> str:
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
            int(bars.weekday[min(t.entry_index, len(bars) - 1)])
        ]

    def news(t) -> str:
        if not calendar_days:
            return "unknown"
        day = bars.local_date[min(t.entry_index, len(bars) - 1)]
        return "news_day" if day in calendar_days else "quiet_day"

    out.by_dimension = {
        "trend_vs_range": bucket(trend_or_range),
        "volatility": bucket(volatility),
        "session": bucket(session),
        "day_of_week": bucket(weekday),
    }
    if calendar_days:
        out.by_dimension["news_day"] = bucket(news)
    return out


# =========================================================== synthetic markets
def bootstrap_bars(bars: Bars, seed: int = 31, block: int = 24) -> Bars:
    """One alternative price history with the same statistical character.

    Stationary block bootstrap of log returns: keeps volatility clustering and
    autocorrelation that an iid shuffle destroys, while producing a path the
    strategy has never seen.
    """
    import pandas as pd

    rng = np.random.default_rng(seed)
    close = bars.close
    if len(close) < block * 3:
        return bars
    log_ret = np.diff(np.log(np.maximum(close, 1e-12)))
    n_blocks = int(np.ceil(len(log_ret) / block))
    starts = rng.integers(0, len(log_ret) - block, size=n_blocks)
    synth_ret = np.concatenate([log_ret[s : s + block] for s in starts])[: len(log_ret)]
    synth_close = close[0] * np.exp(np.cumsum(np.concatenate(([0.0], synth_ret))))

    ratio = synth_close / np.maximum(close, 1e-12)
    df = bars.df.copy()
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].to_numpy() * ratio
    out = Bars(
        symbol=bars.symbol,
        timeframe=bars.timeframe,
        df=df,
        tz=bars.tz,
        source=f"{bars.source}:synthetic[{seed}]",
    )
    return out


@dataclass
class SyntheticResult:
    n_paths: int = 0
    real_net_pnl: float = 0.0
    synthetic_p50: float = 0.0
    synthetic_p95: float = 0.0
    percentile_of_real: float = 0.0
    verdict: str = ""

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}

    def summary(self) -> str:
        return (
            f"[synthetic] real result sits at the {self.percentile_of_real:.0f}th percentile of "
            f"{self.n_paths} bootstrapped histories (median {self.synthetic_p50:,.0f}). {self.verdict}"
        )


def synthetic_market_test(
    run_on: Callable[[Bars], object], bars: Bars, real_net_pnl: float, n_paths: int = 20, seed: int = 31
) -> SyntheticResult:
    nets = []
    for k in range(n_paths):
        synth = bootstrap_bars(bars, seed=seed + k)
        try:
            nets.append(run_on(synth).metrics.net_pnl)
        except Exception:
            continue
    out = SyntheticResult(n_paths=len(nets), real_net_pnl=real_net_pnl)
    if not nets:
        out.verdict = "no synthetic paths completed"
        return out
    arr = np.array(nets)
    out.synthetic_p50 = float(np.percentile(arr, 50))
    out.synthetic_p95 = float(np.percentile(arr, 95))
    out.percentile_of_real = float((arr < real_net_pnl).mean() * 100)
    if out.percentile_of_real >= 95:
        out.verdict = "The real result is better than almost every shuffled history: the edge looks structural."
    elif out.percentile_of_real >= 75:
        out.verdict = "The real result beats most shuffled histories, but not decisively."
    else:
        out.verdict = (
            "Shuffled histories with no edge produce results like this one. That is what you would "
            "expect from noise."
        )
    return out


# ================================================ multiple-comparison correction
@dataclass
class CorrectionResult:
    n_tested: int
    best_metric: float
    corrected_metric: float
    method: str = "deflated"
    note: str = ""

    def to_dict(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.__dict__.items()}

    def summary(self) -> str:
        return (
            f"[selection] {self.n_tested} variant(s) were tested. Best Sharpe {self.best_metric:.2f} "
            f"corrects to {self.corrected_metric:.2f} once selection is accounted for. {self.note}"
        )


def deflated_sharpe(best_sharpe: float, n_trials: int, n_observations: int) -> float:
    """Shrink a selected Sharpe by the expected maximum of n independent trials.

    The expected maximum of n standard normals is approximately
    ``sqrt(2 ln n)``; scaled by the standard error of a Sharpe estimate, that is
    how much of the winner's score is selection rather than skill.
    """
    if n_trials <= 1 or n_observations <= 1:
        return best_sharpe
    expected_max = np.sqrt(2 * np.log(n_trials))
    se = np.sqrt((1 + 0.5 * best_sharpe**2) / max(n_observations - 1, 1))
    return float(best_sharpe - expected_max * se)


def correct_for_selection(best_sharpe: float, n_trials: int, n_observations: int) -> CorrectionResult:
    corrected = deflated_sharpe(best_sharpe, n_trials, n_observations)
    note = (
        "The corrected number is the one to quote."
        if corrected > 0
        else "After correction there is nothing left: this is a selection artefact."
    )
    return CorrectionResult(
        n_tested=n_trials, best_metric=best_sharpe, corrected_metric=corrected, note=note
    )
