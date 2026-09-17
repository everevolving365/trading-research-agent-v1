"""The replay harness (Section 2.2): recorded history through the LIVE path.

No phase is ever gated on real market time. This feeds historical bars through
the same live execution code -- the live plan, the router, the ledger, the
autonomy ladder, the kill switches, the broker adapter -- in accelerated time.

It is the live path, not a simulation of it: the only substitutions are the
clock and the market feed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import numpy as np

from ee_agent.compile.to_live import compile_to_live
from ee_agent.data.bars import Bars
from ee_agent.execution.adapters import BrokerAdapter, MockBroker
from ee_agent.execution.autonomy import AutonomyLadder, Caps, KillSwitchConfig, KillSwitches, Level
from ee_agent.execution.ledger import OrderIntent, PositionLedger
from ee_agent.execution.live_plan import LivePlan
from ee_agent.execution.router import OrderRouter
from ee_agent.instruments.registry import get_instrument
from ee_agent.spec.model import StrategySpec


@dataclass
class ReplayTrade:
    session: str
    side: str
    entry_index: int
    entry_time: str
    entry_price: float
    exit_index: int | None = None
    exit_time: str | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    size: float = 1.0
    pnl: float = 0.0
    fees: float = 0.0

    @property
    def closed(self) -> bool:
        return self.exit_index is not None


@dataclass
class ReplayResult:
    spec_id: str
    symbol: str
    sessions: list[str] = field(default_factory=list)
    trades: list[ReplayTrade] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    kill_switch_trips: list[str] = field(default_factory=list)
    net_pnl: float = 0.0
    orders_submitted: int = 0
    orders_accepted: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def closed_trades(self) -> list[ReplayTrade]:
        return [t for t in self.trades if t.closed]

    def to_dict(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "symbol": self.symbol,
            "n_sessions": len(self.sessions),
            "sessions": self.sessions,
            "n_trades": len(self.closed_trades),
            "net_pnl": round(self.net_pnl, 2),
            "orders_submitted": self.orders_submitted,
            "orders_accepted": self.orders_accepted,
            "refusals": self.refusals,
            "kill_switch_trips": self.kill_switch_trips,
            "notes": self.notes,
        }

    def summary(self) -> str:
        lines = [
            f"[replay] {self.spec_id} on {self.symbol}: {len(self.sessions)} session(s) through the "
            f"live path, {len(self.closed_trades)} trade(s), net {self.net_pnl:,.2f}",
            f"         {self.orders_accepted} of {self.orders_submitted} orders accepted",
        ]
        for r in self.refusals[:5]:
            lines.append(f"         refused: {r}")
        for k in self.kill_switch_trips:
            lines.append(f"         kill switch: {k}")
        return "\n".join(lines)


class ReplayHarness:
    def __init__(
        self,
        spec: StrategySpec,
        adapter: BrokerAdapter | None = None,
        autonomy_level: int = 4,
        account_id: str = "REPLAY-1",
        prop_firm: str | None = None,
        kill_config: KillSwitchConfig | None = None,
        sink: Callable[[str], None] = lambda msg: None,
    ):
        self.spec = spec
        self.instrument = None
        self.adapter = adapter or MockBroker(account_id=account_id, slippage_ticks=1.0)
        self.ladder = AutonomyLadder(
            strategy_id=spec.id, level=Level(autonomy_level), caps=Caps(max_contracts=10, max_trades_per_day=50)
        )
        self.positions = PositionLedger(path=None)
        self.kill = KillSwitches(config=kill_config)
        self.account_id = account_id
        self.prop_firm = prop_firm
        self.sink = sink
        self.live_config = compile_to_live(spec, autonomy_level=autonomy_level)
        self.plan = LivePlan(self.live_config.to_dict())

    def run(self, bars: Bars, max_sessions: int | None = None) -> ReplayResult:
        """Feed bars through the live path, session by session, in accelerated time."""
        self.instrument = get_instrument(bars.symbol)
        self.adapter.connect()
        if hasattr(self.adapter, "tick_size"):
            self.adapter.tick_size = self.instrument.tick_size

        # The replay's own calendar: each session is a distinct trading day, so
        # per-day caps and counters behave exactly as they would live.
        self._current_day = ""
        router = OrderRouter(
            adapter=self.adapter,
            ladder=self.ladder,
            position_ledger=self.positions,
            kill_switches=self.kill,
            prop_firm=self.prop_firm,
            account_id=self.account_id,
            clock=lambda: self._current_day,
            sink=self.sink,
        )

        signals = self.plan.evaluate(bars, self.instrument)
        result = ReplayResult(spec_id=self.spec.id, symbol=bars.symbol)
        sessions = _session_slices(bars)
        if max_sessions:
            sessions = sessions[:max_sessions]

        stop_ticks, target_ticks = self._risk_ticks(bars)
        open_trade: ReplayTrade | None = None

        for day, start, end in sessions:
            result.sessions.append(day)
            self._current_day = day
            for i in range(start, end):
                price = float(bars.close[i])
                self.adapter.set_price(bars.symbol, price) if hasattr(self.adapter, "set_price") else None

                # ---- manage the open position against this bar
                if open_trade is not None:
                    exit_price, reason = self._check_exit(
                        open_trade, bars, i, stop_ticks[i], target_ticks[i]
                    )
                    forced = bool(signals.invalidation[i]) or i == end - 1
                    if exit_price is None and forced:
                        exit_price, reason = price, "session_flatten" if i == end - 1 else "invalidation"
                    if exit_price is not None:
                        intent = OrderIntent(
                            account=self.account_id,
                            symbol=bars.symbol,
                            side="sell" if open_trade.side == "long" else "buy",
                            size=open_trade.size,
                            price=exit_price,
                            strategy_id=self.spec.id,
                            reason=reason,
                        )
                        out = router.submit(intent)
                        result.orders_submitted += 1
                        if out.accepted:
                            result.orders_accepted += 1
                            router.ingest_fills()
                            direction = 1.0 if open_trade.side == "long" else -1.0
                            points = (exit_price - open_trade.entry_price) * direction
                            gross = self.instrument.ticks_to_currency(
                                self.instrument.price_to_ticks(points), open_trade.size
                            )
                            fees = 2 * self.instrument.cost_per_side(exit_price, open_trade.size)
                            open_trade.exit_index = i
                            open_trade.exit_time = bars.df["ts"].iloc[i].isoformat()
                            open_trade.exit_price = exit_price
                            open_trade.exit_reason = reason
                            open_trade.fees = fees
                            open_trade.pnl = gross - fees
                            result.net_pnl += open_trade.pnl
                            router.record_trade_result(open_trade.pnl)
                            open_trade = None
                        else:
                            result.refusals.append(out.message.splitlines()[0])

                # ---- entries
                if open_trade is None and i < end - 1:
                    side = "long" if signals.long_entry[i] else ("short" if signals.short_entry[i] else None)
                    if side:
                        intent = OrderIntent(
                            account=self.account_id,
                            symbol=bars.symbol,
                            side="buy" if side == "long" else "sell",
                            size=float(self.spec.risk.size.value),
                            price=price,
                            strategy_id=self.spec.id,
                            reason="entry",
                        )
                        out = router.submit(intent)
                        result.orders_submitted += 1
                        if out.accepted:
                            result.orders_accepted += 1
                            fills = router.ingest_fills()
                            fill_price = fills[0].price if fills else price
                            open_trade = ReplayTrade(
                                session=day,
                                side=side,
                                entry_index=i,
                                entry_time=bars.df["ts"].iloc[i].isoformat(),
                                entry_price=fill_price,
                                size=float(self.spec.risk.size.value),
                            )
                            result.trades.append(open_trade)
                        else:
                            result.refusals.append(out.message.splitlines()[0])

            if self.kill.is_tripped(strategy_id=self.spec.id, account=self.account_id):
                state = self.kill.is_tripped(strategy_id=self.spec.id, account=self.account_id)
                result.kill_switch_trips.append(f"{state.scope}:{state.name} {state.reason}")
                break

        if open_trade is not None and not open_trade.closed:
            result.notes.append("a position was still open when the replay ended")
        self.adapter.disconnect()
        return result

    # ------------------------------------------------------------- helpers
    def _risk_ticks(self, bars: Bars):
        from ee_agent.engine.backtester import _atr_ticks, _distance_ticks

        atr = _atr_ticks(bars, self.instrument)
        stop = np.array(
            [_distance_ticks(self.spec.risk.stop, self.instrument, bars.close[i], atr[i]) for i in range(len(bars))]
        )
        target = np.array(
            [_distance_ticks(self.spec.risk.target, self.instrument, bars.close[i], atr[i]) for i in range(len(bars))]
        )
        return stop, target

    def _check_exit(self, trade: ReplayTrade, bars: Bars, i: int, stop_ticks: float, target_ticks: float):
        if i <= trade.entry_index:
            return None, ""
        inst = self.instrument
        direction = 1.0 if trade.side == "long" else -1.0
        stop_price = trade.entry_price - direction * inst.ticks_to_price(stop_ticks) if stop_ticks else None
        target_price = trade.entry_price + direction * inst.ticks_to_price(target_ticks) if target_ticks else None
        hit_stop = stop_price is not None and (
            bars.low[i] <= stop_price if trade.side == "long" else bars.high[i] >= stop_price
        )
        hit_target = target_price is not None and (
            bars.high[i] >= target_price if trade.side == "long" else bars.low[i] <= target_price
        )
        if hit_stop:  # ambiguous bars resolve to the stop, exactly as the backtester does
            return float(stop_price), "stop"
        if hit_target:
            return float(target_price), "target"
        return None, ""


def _session_slices(bars: Bars) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    start = 0
    for i in range(1, len(bars)):
        if bars.local_date[i] != bars.local_date[i - 1]:
            out.append((str(bars.local_date[start]), start, i))
            start = i
    if start < len(bars):
        out.append((str(bars.local_date[start]), start, len(bars)))
    return out
