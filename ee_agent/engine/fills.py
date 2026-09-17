"""The fill model: partial fills, queue position and stop-run modelling.

The pessimistic assumptions are deliberate. Where a bar is ambiguous -- price
touched both the stop and the target inside the same bar -- the model assumes
the stop. That single rule removes the most common way a backtest flatters
itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from ee_agent.engine.costs import CostModel

Side = Literal["long", "short"]


@dataclass
class Fill:
    price: float
    size: float
    commission: float
    slippage_ticks: float
    reason: str
    partial: bool = False

    def to_dict(self) -> dict:
        return {
            "price": round(self.price, 8),
            "size": self.size,
            "commission": round(self.commission, 6),
            "slippage_ticks": round(self.slippage_ticks, 4),
            "reason": self.reason,
            "partial": self.partial,
        }


class FillModel:
    def __init__(self, costs: CostModel, max_participation: float = 0.10, queue_factor: float = 0.5):
        self.costs = costs
        self.instrument = costs.instrument
        #: you may not take more than this share of a bar's volume
        self.max_participation = max_participation
        #: probability a resting limit order at the touch actually fills
        self.queue_factor = queue_factor

    # ------------------------------------------------------------- sizing
    def fillable_size(self, wanted: float, bar_volume: float) -> tuple[float, bool]:
        """Partial fills: a bar cannot give you more than a share of its volume."""
        if bar_volume <= 0:
            return 0.0, True
        cap = max(self.max_participation * bar_volume, 0.0)
        if wanted <= cap:
            return wanted, False
        got = float(np.floor(cap)) if self.instrument.asset_class in ("future",) else cap
        return max(got, 0.0), True

    # -------------------------------------------------------------- market
    def market(
        self,
        side: Side,
        reference_price: float,
        size: float,
        bar_volume: float,
        atr_ticks: float,
        reason: str = "entry",
        is_stop: bool = False,
    ) -> Fill:
        got, partial = self.fillable_size(size, bar_volume)
        slip_ticks = self.costs.slippage_for(
            size=max(got, size),
            bar_volume=bar_volume,
            atr_ticks=atr_ticks,
            typical_volume=self.instrument.typical_daily_volume,
            is_stop=is_stop,
        )
        adverse = slip_ticks + self.costs.half_spread_ticks()
        direction = 1.0 if side == "long" else -1.0
        price = reference_price + direction * self.instrument.ticks_to_price(adverse)
        return Fill(
            price=self.instrument.round_to_tick(price),
            size=got,
            commission=self.costs.commission(price, got),
            slippage_ticks=adverse,
            reason=reason,
            partial=partial,
        )

    # --------------------------------------------------------------- limit
    def limit(
        self,
        side: Side,
        limit_price: float,
        size: float,
        bar_high: float,
        bar_low: float,
        bar_volume: float,
        reason: str = "target",
    ) -> Fill | None:
        """Queue position: touching the level is not the same as being filled.

        A limit only fills if price traded *through* it, or traded to it with
        enough volume behind it that the queue plausibly cleared.
        """
        through = (side == "long" and bar_low < limit_price) or (side == "short" and bar_high > limit_price)
        touched = bar_low <= limit_price <= bar_high
        if not touched:
            return None
        if not through:
            depth = bar_volume * self.queue_factor
            if depth < size:
                return None
        got, partial = self.fillable_size(size, bar_volume)
        if got <= 0:
            return None
        return Fill(
            price=self.instrument.round_to_tick(limit_price),
            size=got,
            commission=self.costs.commission(limit_price, got),
            slippage_ticks=0.0,
            reason=reason,
            partial=partial,
        )

    # ---------------------------------------------------------------- stop
    def stop(
        self,
        side: Side,
        stop_price: float,
        size: float,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_volume: float,
        atr_ticks: float,
        reason: str = "stop",
    ) -> Fill | None:
        """Stop-run modelling.

        A stop triggers on the touch and then fills at market, into the move.
        If the bar *gapped* through the stop, the fill is at the open -- there
        was never a chance to get the stop price.
        """
        exit_side: Side = "short" if side == "long" else "long"
        triggered = (side == "long" and bar_low <= stop_price) or (side == "short" and bar_high >= stop_price)
        if not triggered:
            return None
        gapped = (side == "long" and bar_open < stop_price) or (side == "short" and bar_open > stop_price)
        reference = bar_open if gapped else stop_price
        fill = self.market(
            side=exit_side,
            reference_price=reference,
            size=size,
            bar_volume=bar_volume,
            atr_ticks=atr_ticks,
            reason=reason + (":gap" if gapped else ""),
            is_stop=True,
        )
        if fill.size <= 0:  # no volume: you are still in, and that is the honest answer
            return None
        return fill

    def ambiguous_bar_resolution(
        self, side: Side, stop_price: float | None, target_price: float | None, bar_high: float, bar_low: float
    ) -> str:
        """Which came first inside the bar? Without tick data nobody knows, so
        the model assumes the stop. Reported, never silent."""
        if stop_price is None or target_price is None:
            return "unambiguous"
        hit_stop = (side == "long" and bar_low <= stop_price) or (side == "short" and bar_high >= stop_price)
        hit_target = (side == "long" and bar_high >= target_price) or (
            side == "short" and bar_low <= target_price
        )
        if hit_stop and hit_target:
            return "both_in_bar:assumed_stop_first"
        return "unambiguous"
