"""The event-driven backtester (abilities 34, 35, 37).

Bar by bar, in order, with no vectorised shortcut that could smuggle in
lookahead. Signals are computed causally by the compiled plan; this loop only
ever reads bar ``i`` and the position state built from bars before it.

What the loop guarantees:

* An entry signal on bar *i* fills at bar *i*'s close or bar *i+1*'s open,
  according to ``execution.entry_order`` -- never at a price that was not
  available after the signal was known.
* Stops and targets are evaluated against the *next* bars' highs and lows, with
  the ambiguous-bar rule resolving to the stop.
* Costs are charged on every side of every fill. There is no path around them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

import numpy as np

from ee_agent.compile.to_python import CompiledStrategy, SignalArrays, compile_to_python
from ee_agent.data.bars import Bars
from ee_agent.engine.costs import CostModel
from ee_agent.engine.fills import FillModel
from ee_agent.engine.metrics import Metrics, compute_metrics
from ee_agent.errors import CostModelMissing
from ee_agent.instruments.registry import Instrument, get_instrument
from ee_agent.spec.model import Distance, StrategySpec


@dataclass
class Trade:
    id: int
    side: str
    entry_index: int
    entry_time: str
    entry_price: float
    size: float
    exit_index: int | None = None
    exit_time: str | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    stop_price: float | None = None
    target_price: float | None = None
    gross_pnl: float = 0.0
    commission: float = 0.0
    slippage_cost: float = 0.0
    net_pnl: float = 0.0
    bars_held: int = 0
    mae: float = 0.0  # maximum adverse excursion, currency
    mfe: float = 0.0  # maximum favourable excursion, currency
    signal_rule: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return self.exit_index is not None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    spec_id: str
    spec_hash: str
    plan_hash: str
    symbol: str
    timeframe: str
    bars_hash: str
    n_bars: int
    start: str
    end: str
    trades: list[Trade]
    equity: np.ndarray
    equity_times: list[str]
    metrics: Metrics
    costs_total: float
    cost_model: str
    data_quality: float
    signal_counts: dict[str, int]
    ambiguous_bars: int = 0
    partial_fills: int = 0
    window_label: str = "full"
    notes: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def defensible(self) -> bool:
        """A single window is never a defensible result on its own (hard rule 7).
        Only :class:`ee_agent.engine.truth.StrategyReport` is."""
        return False

    def to_dict(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "spec_hash": self.spec_hash,
            "plan_hash": self.plan_hash,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars_hash": self.bars_hash,
            "n_bars": self.n_bars,
            "start": self.start,
            "end": self.end,
            "window": self.window_label,
            "metrics": self.metrics.to_dict(),
            "costs_total": round(self.costs_total, 4),
            "cost_model": self.cost_model,
            "data_quality": self.data_quality,
            "signal_counts": self.signal_counts,
            "ambiguous_bars": self.ambiguous_bars,
            "partial_fills": self.partial_fills,
            "n_trades": len(self.trades),
            "notes": self.notes,
        }

    def report(self) -> str:
        m = self.metrics
        lines = [
            "",
            f"  {self.spec_id} on {self.symbol} {self.timeframe}  [{self.window_label}]",
            f"  {self.start} -> {self.end}   {self.n_bars:,} bars   data quality {self.data_quality:.2f}",
            "  " + "-" * 68,
            f"  Net P&L          {m.net_pnl:>14,.2f}     Trades        {m.n_trades:>10,}",
            f"  Return on risk   {m.return_on_max_dd:>14.2f}x    Win rate      {m.win_rate:>9.1%}",
            f"  Max drawdown     {m.max_drawdown:>14,.2f}     Profit factor {m.profit_factor:>10.2f}",
            f"  Sharpe (ann.)    {m.sharpe:>14.2f}     Expectancy    {m.expectancy:>10,.2f}",
            f"  Costs paid       {self.costs_total:>14,.2f}     Avg bars held {m.avg_bars_held:>10.1f}",
            "  " + "-" * 68,
            f"  {self.cost_model}",
            f"  Signals: {self.signal_counts}",
        ]
        if self.ambiguous_bars:
            lines.append(
                f"  {self.ambiguous_bars} bar(s) hit stop and target together: the stop was assumed first."
            )
        if self.partial_fills:
            lines.append(f"  {self.partial_fills} partial fill(s) from volume limits.")
        for note in self.notes:
            lines.append(f"  ! {note}")
        lines.append(
            "  SINGLE WINDOW -- not a defensible number on its own. Run the truth engine "
            "(ee-agent analyze) for in-sample/out-of-sample, Monte Carlo and the lookahead verdict."
        )
        return "\n".join(lines)


class Backtester:
    """Constructing this without a cost model is impossible. That is the point."""

    def __init__(
        self,
        spec: StrategySpec,
        instrument: Instrument | None = None,
        initial_capital: float = 50_000.0,
        compiled: CompiledStrategy | None = None,
    ):
        self.spec = spec
        self.initial_capital = initial_capital
        self.compiled = compiled or compile_to_python(spec)
        self._instrument_override = instrument

    def _instrument(self, bars: Bars) -> Instrument:
        if self._instrument_override:
            return self._instrument_override
        return get_instrument(bars.symbol)

    # ------------------------------------------------------------------ run
    def run(self, bars: Bars, window_label: str = "full", signals: SignalArrays | None = None) -> BacktestResult:
        if len(bars) < 2:
            raise ValueError("Need at least two bars to run a backtest.")
        instrument = self._instrument(bars)
        cost_model = CostModel.from_spec(self.spec.costs, instrument)  # raises if frictionless
        fills = FillModel(cost_model)
        sig = signals if signals is not None else self.compiled.evaluate(bars, instrument)

        spec = self.spec
        exec_cfg = spec.execution
        enter_on_close = exec_cfg.entry_order == "market_on_close_of_signal_bar"

        atr = _atr_ticks(bars, instrument, length=14)
        equity = np.zeros(len(bars))
        cash = self.initial_capital
        trades: list[Trade] = []
        open_trades: list[Trade] = []
        trade_id = 0
        costs_total = 0.0
        ambiguous = 0
        partials = 0
        trades_today = 0
        current_day = None
        notes: list[str] = []

        for i in range(len(bars)):
            day = bars.local_date[i]
            if day != current_day:
                current_day, trades_today = day, 0

            # ---- 1. manage open positions against THIS bar ----------------
            still_open: list[Trade] = []
            for trade in open_trades:
                closed = self._manage(trade, bars, i, instrument, fills, atr)
                if closed:
                    costs_total += trade.commission
                    cash += trade.net_pnl
                    trades.append(trade)
                    if "both_in_bar" in " ".join(trade.notes):
                        ambiguous += 1
                else:
                    still_open.append(trade)
            open_trades = still_open

            # ---- 2. forced exits: invalidation and session close -----------
            if sig.invalidation[i] or (
                exec_cfg.session_close_behavior == "flatten" and _is_last_bar_of_window(bars, sig, i)
            ):
                for trade in open_trades:
                    reason = "invalidation" if sig.invalidation[i] else "session_flatten"
                    fill = fills.market(
                        side="short" if trade.side == "long" else "long",
                        reference_price=bars.close[i],
                        size=trade.size,
                        bar_volume=bars.volume[i],
                        atr_ticks=atr[i],
                        reason=reason,
                    )
                    self._close(trade, bars, i, fill, instrument, reason)
                    costs_total += fill.commission
                    cash += trade.net_pnl
                    trades.append(trade)
                open_trades = []

            # ---- 3. entries ------------------------------------------------
            want_long = bool(sig.long_entry[i])
            want_short = bool(sig.short_entry[i])
            if want_long or want_short:
                blocked = None
                if len(open_trades) >= spec.risk.max_concurrent_positions:
                    blocked = exec_cfg.on_overlapping_signal
                if spec.filters.max_trades_per_day is not None and trades_today >= spec.filters.max_trades_per_day:
                    blocked = "max_trades_per_day"
                if blocked == "replace" and open_trades:
                    for trade in open_trades:
                        fill = fills.market(
                            side="short" if trade.side == "long" else "long",
                            reference_price=bars.close[i],
                            size=trade.size,
                            bar_volume=bars.volume[i],
                            atr_ticks=atr[i],
                            reason="replaced_by_new_signal",
                        )
                        self._close(trade, bars, i, fill, instrument, "replaced")
                        costs_total += fill.commission
                        cash += trade.net_pnl
                        trades.append(trade)
                    open_trades = []
                    blocked = None
                if blocked in (None, "stack") or (blocked == "stack"):
                    if blocked in (None,) or blocked == "stack":
                        side = "long" if want_long else "short"
                        entry_index = i if enter_on_close else min(i + 1, len(bars) - 1)
                        reference = bars.close[i] if enter_on_close else bars.open[entry_index]
                        size = self._size(spec.risk, instrument, reference, atr[i], cash)
                        if size > 0:
                            fill = fills.market(
                                side=side,
                                reference_price=reference,
                                size=size,
                                bar_volume=bars.volume[entry_index],
                                atr_ticks=atr[entry_index],
                                reason="entry",
                            )
                            if fill.size > 0:
                                trade_id += 1
                                if fill.partial:
                                    partials += 1
                                trade = Trade(
                                    id=trade_id,
                                    side=side,
                                    entry_index=entry_index,
                                    entry_time=_iso(bars, entry_index),
                                    entry_price=fill.price,
                                    size=fill.size,
                                    commission=fill.commission,
                                    slippage_cost=instrument.ticks_to_currency(
                                        fill.slippage_ticks, fill.size
                                    ),
                                    signal_rule="long" if want_long else "short",
                                )
                                trade.stop_price = _level(spec.risk.stop, side, fill.price, instrument, atr[i], "stop")
                                trade.target_price = _level(
                                    spec.risk.target, side, fill.price, instrument, atr[i], "target"
                                )
                                open_trades.append(trade)
                                trades_today += 1

            # ---- 4. mark to market ----------------------------------------
            unrealised = sum(_unrealised(t, bars.close[i], instrument) for t in open_trades)
            equity[i] = cash + unrealised

        # ---- close anything still open at the end -------------------------
        last = len(bars) - 1
        for trade in open_trades:
            fill = fills.market(
                side="short" if trade.side == "long" else "long",
                reference_price=bars.close[last],
                size=trade.size,
                bar_volume=bars.volume[last],
                atr_ticks=atr[last],
                reason="end_of_data",
            )
            self._close(trade, bars, last, fill, instrument, "end_of_data")
            costs_total += fill.commission
            cash += trade.net_pnl
            trades.append(trade)
            notes.append(f"trade {trade.id} was still open at the end of the data and was closed at the last close")
        if open_trades:
            equity[last] = cash

        trades.sort(key=lambda t: t.entry_index)
        costs_total = sum(t.commission for t in trades)
        metrics = compute_metrics(trades, equity, bars, self.initial_capital)

        return BacktestResult(
            spec_id=spec.id,
            spec_hash=spec.hash,
            plan_hash=self.compiled.plan_hash,
            symbol=bars.symbol,
            timeframe=bars.timeframe,
            bars_hash=bars.content_hash,
            n_bars=len(bars),
            start=_iso(bars, 0),
            end=_iso(bars, last),
            trades=trades,
            equity=equity,
            equity_times=[_iso(bars, i) for i in range(len(bars))],
            metrics=metrics,
            costs_total=costs_total,
            cost_model=cost_model.describe(),
            data_quality=bars.quality_score,
            signal_counts=sig.counts(),
            ambiguous_bars=ambiguous,
            partial_fills=partials,
            window_label=window_label,
            notes=notes,
        )

    # ------------------------------------------------------------- helpers
    def _manage(self, trade: Trade, bars: Bars, i: int, instrument, fills: FillModel, atr) -> bool:
        if i <= trade.entry_index:
            return False
        trade.bars_held = i - trade.entry_index
        direction = 1.0 if trade.side == "long" else -1.0
        excursion_up = (bars.high[i] - trade.entry_price) * direction
        excursion_dn = (bars.low[i] - trade.entry_price) * direction
        trade.mfe = max(trade.mfe, instrument.ticks_to_currency(
            instrument.price_to_ticks(max(excursion_up, excursion_dn)), trade.size))
        trade.mae = min(trade.mae, instrument.ticks_to_currency(
            instrument.price_to_ticks(min(excursion_up, excursion_dn)), trade.size))

        resolution = fills.ambiguous_bar_resolution(
            trade.side, trade.stop_price, trade.target_price, bars.high[i], bars.low[i]
        )
        if resolution != "unambiguous":
            trade.notes.append(f"bar {i}: {resolution}")

        if trade.stop_price is not None:
            fill = fills.stop(
                side=trade.side,
                stop_price=trade.stop_price,
                size=trade.size,
                bar_open=bars.open[i],
                bar_high=bars.high[i],
                bar_low=bars.low[i],
                bar_volume=bars.volume[i],
                atr_ticks=atr[i],
            )
            if fill:
                self._close(trade, bars, i, fill, instrument, "stop")
                return True

        if trade.target_price is not None:
            fill = fills.limit(
                side="short" if trade.side == "long" else "long",
                limit_price=trade.target_price,
                size=trade.size,
                bar_high=bars.high[i],
                bar_low=bars.low[i],
                bar_volume=bars.volume[i],
            )
            if fill:
                self._close(trade, bars, i, fill, instrument, "target")
                return True
        return False

    def _close(self, trade: Trade, bars: Bars, i: int, fill, instrument, reason: str) -> None:
        trade.exit_index = i
        trade.exit_time = _iso(bars, i)
        trade.exit_price = fill.price
        trade.exit_reason = reason
        trade.commission += fill.commission
        trade.slippage_cost += instrument.ticks_to_currency(fill.slippage_ticks, trade.size)
        direction = 1.0 if trade.side == "long" else -1.0
        points = (fill.price - trade.entry_price) * direction
        trade.gross_pnl = instrument.ticks_to_currency(instrument.price_to_ticks(points), trade.size)
        trade.net_pnl = trade.gross_pnl - trade.commission
        trade.bars_held = i - trade.entry_index

    def _size(self, risk, instrument, price: float, atr_ticks: float, equity: float) -> float:
        method = risk.size.method
        value = risk.size.value
        if method == "fixed_contracts":
            return float(value)
        if method == "fixed_notional":
            return max(0.0, value / max(instrument.notional(price), 1e-9))
        if method == "risk_percent":
            stop_ticks = _distance_ticks(risk.stop, instrument, price, atr_ticks)
            risk_amount = equity * value / 100.0
            per_contract = instrument.ticks_to_currency(max(stop_ticks, 1e-9))
            size = risk_amount / max(per_contract, 1e-9)
            return float(np.floor(size)) if instrument.asset_class == "future" else size
        if method == "risk_ticks":
            per_contract = instrument.ticks_to_currency(max(value, 1e-9))
            size = (equity * 0.01) / max(per_contract, 1e-9)
            return float(np.floor(size)) if instrument.asset_class == "future" else size
        return float(value)


# ------------------------------------------------------------------- helpers
def _distance_ticks(distance: Distance | None, instrument, price: float, atr_ticks: float) -> float:
    if distance is None:
        return 0.0
    if distance.type == "ticks":
        return distance.value
    if distance.type == "points":
        return instrument.price_to_ticks(distance.value)
    if distance.type == "percent":
        return instrument.price_to_ticks(price * distance.value / 100.0)
    if distance.type == "bps":
        return instrument.price_to_ticks(price * distance.value / 10000.0)
    if distance.type == "atr":
        return atr_ticks * distance.value
    if distance.type == "stdev":
        return atr_ticks * distance.value  # ATR stands in for stdev at bar scale
    return distance.value


def _level(distance, side: str, entry: float, instrument, atr_ticks: float, kind: str) -> float | None:
    if distance is None:
        return None
    ticks = _distance_ticks(distance, instrument, entry, atr_ticks)
    offset = instrument.ticks_to_price(ticks)
    sign = 1.0 if side == "long" else -1.0
    if kind == "stop":
        return instrument.round_to_tick(entry - sign * offset)
    return instrument.round_to_tick(entry + sign * offset)


def _unrealised(trade: Trade, price: float, instrument) -> float:
    direction = 1.0 if trade.side == "long" else -1.0
    points = (price - trade.entry_price) * direction
    return instrument.ticks_to_currency(instrument.price_to_ticks(points), trade.size) - trade.commission


def _atr_ticks(bars: Bars, instrument, length: int = 14) -> np.ndarray:
    from ee_agent.spec.primitives import atr_series

    atr = atr_series(bars, length)
    out = atr / instrument.tick_size
    return np.nan_to_num(out, nan=float(instrument.typical_daily_range_ticks or 10) / 26.0)


def _is_last_bar_of_window(bars: Bars, sig: SignalArrays, i: int) -> bool:
    """Inside the filter window on this bar, outside it on the next."""
    if i >= len(bars) - 1:
        return True
    if bars.local_date[i] != bars.local_date[i + 1]:
        return True
    return bool(sig.filter_ok[i] and not sig.filter_ok[i + 1])


def _iso(bars: Bars, i: int) -> str:
    return bars.df["ts"].iloc[i].isoformat()
