"""Performance metrics. Every number here is defined, not assumed."""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np


@dataclass
class Metrics:
    n_trades: int = 0
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    costs: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    return_on_max_dd: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    cagr: float = 0.0
    total_return_pct: float = 0.0
    avg_bars_held: float = 0.0
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0
    exposure_pct: float = 0.0
    trades_per_day: float = 0.0
    avg_mae: float = 0.0
    avg_mfe: float = 0.0
    long_trades: int = 0
    short_trades: int = 0
    long_pnl: float = 0.0
    short_pnl: float = 0.0

    def to_dict(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(self).items()}

    def headline(self) -> str:
        return (
            f"{self.n_trades} trades, net {self.net_pnl:,.2f}, max DD {self.max_drawdown:,.2f}, "
            f"PF {self.profit_factor:.2f}, win {self.win_rate:.1%}, Sharpe {self.sharpe:.2f}"
        )


def max_drawdown(equity: np.ndarray) -> tuple[float, float]:
    if len(equity) == 0:
        return 0.0, 0.0
    peaks = np.maximum.accumulate(equity)
    dd = equity - peaks
    trough = int(np.argmin(dd))
    worst = float(dd[trough])
    peak_value = float(peaks[trough]) if peaks[trough] else 1.0
    return abs(worst), abs(worst / peak_value) * 100.0 if peak_value else 0.0


def compute_metrics(trades, equity: np.ndarray, bars, initial_capital: float) -> Metrics:
    m = Metrics()
    closed = [t for t in trades if t.closed]
    m.n_trades = len(closed)
    if len(equity) == 0:
        return m

    pnls = np.array([t.net_pnl for t in closed], dtype=float) if closed else np.array([])
    m.net_pnl = float(pnls.sum()) if len(pnls) else 0.0
    m.gross_pnl = float(sum(t.gross_pnl for t in closed))
    m.costs = float(sum(t.commission for t in closed))

    if len(pnls):
        wins, losses = pnls[pnls > 0], pnls[pnls < 0]
        m.win_rate = len(wins) / len(pnls)
        m.avg_win = float(wins.mean()) if len(wins) else 0.0
        m.avg_loss = float(losses.mean()) if len(losses) else 0.0
        m.largest_win = float(wins.max()) if len(wins) else 0.0
        m.largest_loss = float(losses.min()) if len(losses) else 0.0
        gross_win, gross_loss = float(wins.sum()), abs(float(losses.sum()))
        m.profit_factor = gross_win / gross_loss if gross_loss > 0 else (np.inf if gross_win > 0 else 0.0)
        m.expectancy = float(pnls.mean())
        m.avg_bars_held = float(np.mean([t.bars_held for t in closed]))
        m.avg_mae = float(np.mean([t.mae for t in closed]))
        m.avg_mfe = float(np.mean([t.mfe for t in closed]))
        m.max_consecutive_losses = _max_run(pnls < 0)
        m.max_consecutive_wins = _max_run(pnls > 0)
        m.long_trades = sum(1 for t in closed if t.side == "long")
        m.short_trades = sum(1 for t in closed if t.side == "short")
        m.long_pnl = float(sum(t.net_pnl for t in closed if t.side == "long"))
        m.short_pnl = float(sum(t.net_pnl for t in closed if t.side == "short"))
        held = sum(t.bars_held for t in closed)
        m.exposure_pct = 100.0 * held / max(len(equity), 1)

    m.max_drawdown, m.max_drawdown_pct = max_drawdown(equity)
    m.return_on_max_dd = m.net_pnl / m.max_drawdown if m.max_drawdown > 0 else 0.0
    m.total_return_pct = 100.0 * m.net_pnl / initial_capital if initial_capital else 0.0

    # returns-based statistics, annualised from the bar spacing
    rets = np.diff(equity) / max(initial_capital, 1e-9)
    if len(rets) > 2 and np.std(rets) > 0:
        from ee_agent.data.bars import timeframe_minutes

        per_year = (365.25 * 24 * 60) / max(timeframe_minutes(bars.timeframe), 1e-9)
        ann = np.sqrt(per_year)
        m.sharpe = float(np.mean(rets) / np.std(rets) * ann)
        downside = rets[rets < 0]
        m.sortino = float(np.mean(rets) / np.std(downside) * ann) if len(downside) > 1 and np.std(downside) > 0 else 0.0

    days = _span_days(bars)
    if days > 0:
        m.trades_per_day = m.n_trades / days
        years = days / 365.25
        if years > 0 and initial_capital > 0:
            ending = initial_capital + m.net_pnl
            if ending > 0:
                m.cagr = float(((ending / initial_capital) ** (1 / years) - 1) * 100.0)
    return m


def _max_run(flags: np.ndarray) -> int:
    best = run = 0
    for flag in flags:
        run = run + 1 if flag else 0
        best = max(best, run)
    return int(best)


def _span_days(bars) -> float:
    if len(bars) < 2:
        return 0.0
    first, last = bars.first_bar_time, bars.last_bar_time
    return max((last - first).total_seconds() / 86400.0, 0.0)
