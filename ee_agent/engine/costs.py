"""Execution realism: costs, spread and slippage. Always on (ability 37).

Hard rule 7: no backtest without a cost model. There is no flag to turn this
off, and :class:`ee_agent.engine.backtester.Backtester` refuses to construct
without one.

Slippage is a function of volume and volatility, not a constant, because a
constant is what makes an illiquid strategy look tradeable.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ee_agent.errors import CostModelMissing
from ee_agent.instruments.registry import Instrument
from ee_agent.spec.model import Costs


@dataclass
class CostModel:
    instrument: Instrument
    commission_per_side: float
    slippage_ticks: float
    spread_ticks: float
    model: str = "volume_volatility"
    exchange_fees_per_side: float = 0.0

    @classmethod
    def from_spec(cls, costs: Costs, instrument: Instrument) -> "CostModel":
        if costs is None:
            raise CostModelMissing("A spec reached the engine with no costs block.")
        spread = costs.spread_ticks or instrument.spread_ticks_typical
        model = cls(
            instrument=instrument,
            commission_per_side=costs.commission_per_side,
            slippage_ticks=costs.slippage_ticks,
            spread_ticks=spread,
            model=costs.slippage_model,
            exchange_fees_per_side=costs.exchange_fees_per_side or instrument.fees_per_side,
        )
        if model.is_frictionless:
            raise CostModelMissing(
                "commission, slippage and spread are all zero. A frictionless backtest is not a "
                "number anyone can defend (Law One). Set costs in the spec."
            )
        return model

    @property
    def is_frictionless(self) -> bool:
        return (
            self.commission_per_side == 0
            and self.slippage_ticks == 0
            and self.spread_ticks == 0
            and self.exchange_fees_per_side == 0
            and self.instrument.fee_bps == 0
        )

    # -------------------------------------------------------------- charges
    def commission(self, price: float, size: float) -> float:
        """One side. Includes exchange fees and percentage fees where they apply."""
        cost = (self.commission_per_side + self.exchange_fees_per_side) * size
        if self.instrument.fee_bps:
            cost += price * size * self.instrument.multiplier * self.instrument.fee_bps / 10000.0
        if self.instrument.commission_per_share:
            cost += self.instrument.commission_per_share * size
        return cost

    def half_spread_ticks(self) -> float:
        return self.spread_ticks / 2.0

    def slippage_for(
        self,
        size: float,
        bar_volume: float,
        atr_ticks: float,
        typical_volume: float,
        is_stop: bool = False,
    ) -> float:
        """Slippage in ticks for one side.

        Three components, all in ticks:
          base        -- the spec's stated slippage
          volume term -- grows with participation: how much of the bar you are
          volatility  -- grows with ATR relative to the instrument's normal range

        Stop orders pay more: they execute into the move that triggered them.
        """
        base = self.slippage_ticks
        if self.model == "fixed":
            return base * (1.6 if is_stop else 1.0)

        participation = 0.0
        if bar_volume > 0:
            participation = min(size / max(bar_volume, 1e-9), 1.0)
        volume_term = 4.0 * np.sqrt(participation)

        vol_term = 0.0
        if typical_volume > 0 and atr_ticks > 0:
            normal_atr_ticks = max(self.instrument.typical_daily_range_ticks / 26.0, 1e-9)
            vol_term = 0.5 * max(atr_ticks / normal_atr_ticks - 1.0, 0.0)

        total = base + volume_term + vol_term
        if is_stop:
            total *= 1.6
        return float(total)

    def total_round_turn_estimate(self, price: float, size: float = 1.0) -> float:
        """What one round turn costs before the market moves at all. The number
        a client should see next to any average-trade figure."""
        ticks = 2 * (self.slippage_ticks + self.half_spread_ticks())
        return 2 * self.commission(price, size) + self.instrument.ticks_to_currency(ticks, size)

    def describe(self) -> str:
        return (
            f"[costs] {self.instrument.symbol}: commission {self.commission_per_side:.2f}/side "
            f"+ fees {self.exchange_fees_per_side:.2f}/side, spread {self.spread_ticks:.2f} ticks, "
            f"slippage {self.slippage_ticks:.2f} ticks base ({self.model}); "
            f"round turn ~{self.total_round_turn_estimate(1.0):.2f} {self.instrument.currency} + "
            f"{self.instrument.ticks_to_currency(2 * (self.slippage_ticks + self.half_spread_ticks())):.2f} "
            "market impact"
        )
