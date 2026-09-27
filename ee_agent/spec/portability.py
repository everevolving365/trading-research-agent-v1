"""Portability: making one spec run on any asset without editing it.

Section 4 requires every parameter to be dimensionless. Two of the allowed
units are not actually scale-free across instruments:

* ``ticks``  -- a tick is an instrument-specific quantity, so 100 ticks means
  something different on MNQ (25 points) than on EURUSD (10 pips).
* ``points`` -- a point is a price unit, so 25 points is 0.12% of MNQ and
  0.04% of Bitcoin.

``percent``, ``atr`` and ``stdev`` are scale-free: they mean the same thing
everywhere.

The agent never rewrites the client's risk (hard rule 1). What it does is
*measure* the conversion, show it, and apply it only with the client's explicit
approval -- recorded in the ambiguity ledger like any other assumption.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ee_agent.instruments.registry import get_instrument
from ee_agent.spec.model import Assumption, Distance, StrategySpec

SCALE_FREE_UNITS = {"percent", "atr", "stdev", "bps"}

#: Condition parameters that are price distances. A strategy's entry rule can
#: carry a distance just like its stop does (NASH's "gap within 120 points of
#: the level"), and 120 points means something different on every instrument.
CONDITION_DISTANCE_KEYS = ("max_distance",)


def condition_distances(spec: StrategySpec) -> list[tuple[str, object, str, Distance]]:
    """(field label, condition, parameter key, distance) for every distance
    parameter inside a signal condition."""
    from ee_agent.spec.primitives import _distance_param

    out = []
    for rule in [*spec.signals.entry, *spec.signals.exit]:
        for cond in rule.conditions:
            for key in CONDITION_DISTANCE_KEYS:
                if key in cond.params:
                    unit, value = _distance_param(cond.params[key])
                    out.append((f"{rule.id}.{cond.type}.{key}", cond, key, Distance(type=unit, value=value)))
    return out


@dataclass
class ConversionRow:
    field: str
    original: str
    converted: str
    reference_symbol: str
    note: str


def is_portable(spec: StrategySpec) -> bool:
    for dist in (spec.risk.stop, spec.risk.target, spec.risk.trail, spec.risk.breakeven_at):
        if dist is not None and dist.type not in SCALE_FREE_UNITS:
            return False
    return all(d.type in SCALE_FREE_UNITS for _l, _c, _k, d in condition_distances(spec))


def portability_note(spec: StrategySpec) -> str | None:
    """Exactly what differs and by how much, per instrument in the universe."""
    if is_portable(spec):
        return None
    rows: list[str] = []
    for symbol in spec.universe.instruments:
        try:
            inst = get_instrument(symbol)
        except KeyError:
            continue
        distances = [("stop", spec.risk.stop), ("target", spec.risk.target)]
        distances += [(label, d) for label, _c, _k, d in condition_distances(spec)]
        for label, dist in distances:
            if dist is None or dist.type in SCALE_FREE_UNITS:
                continue
            ticks = dist.value / inst.tick_size if dist.type == "points" else dist.value
            daily = inst.typical_daily_range_ticks or 1.0
            rows.append(
                f"    {symbol:<8} {label}: {dist.value} {dist.type} = {ticks:,.0f} ticks "
                f"= {ticks / daily:.1%} of a typical day's range"
            )
    if not rows:
        return None
    return (
        "This spec's distances are in units that do NOT mean the same thing on every instrument:\n"
        + "\n".join(rows)
        + "\nRunning it unedited across these instruments changes the strategy. "
        "`ee-agent spec portable <spec>` measures the equivalent ATR multiples and shows them "
        "for your approval; nothing is changed without it."
    )


def measure_atr_multiple(bars, distance: Distance, atr_length: int = 14) -> float:
    """What the stated distance is worth in ATR multiples on this dataset."""
    from ee_agent.spec.primitives import atr_series

    inst = get_instrument(bars.symbol)
    atr = atr_series(bars, atr_length)
    median_atr = float(np.nanmedian(atr))
    if not np.isfinite(median_atr) or median_atr <= 0:
        raise ValueError("Cannot measure ATR on this dataset.")
    if distance.type == "points":
        price_distance = distance.value
    elif distance.type == "ticks":
        price_distance = distance.value * inst.tick_size
    elif distance.type == "percent":
        price_distance = float(np.nanmedian(bars.close)) * distance.value / 100.0
    elif distance.type == "bps":
        price_distance = float(np.nanmedian(bars.close)) * distance.value / 10000.0
    elif distance.type == "atr":
        return distance.value
    else:
        price_distance = distance.value
    return price_distance / median_atr


def to_atr_units(
    spec: StrategySpec, bars, approved_by: str | None = None, atr_length: int = 14
) -> tuple[StrategySpec, list[ConversionRow]]:
    """Return a converted copy plus the conversion table.

    ``approved_by=None`` produces the proposal with the assumption left
    unresolved, which the validator treats as blocking. Nothing reaches live
    capital on an unapproved conversion.
    """
    out = StrategySpec.from_dict(spec.to_dict())
    rows: list[ConversionRow] = []
    for label in ("stop", "target", "trail", "breakeven_at"):
        dist = getattr(spec.risk, label)
        if dist is None or dist.type in ("atr",):
            continue
        multiple = round(measure_atr_multiple(bars, dist, atr_length), 4)
        setattr(out.risk, label, Distance(type="atr", value=multiple))
        rows.append(
            ConversionRow(
                field=f"risk.{label}",
                original=f"{dist.value} {dist.type}",
                converted=f"{multiple} ATR({atr_length})",
                reference_symbol=bars.symbol,
                note=(
                    f"measured against the median ATR({atr_length}) of {bars.symbol} "
                    f"{bars.timeframe} over {len(bars):,} bars"
                ),
            )
        )
    for label, cond, key, dist in condition_distances(out):
        if dist.type in ("atr",):
            continue
        # condition distances in ATR are always ATR(14) -- see primitives._distance_series
        multiple = round(measure_atr_multiple(bars, dist, 14), 4)
        cond.params[key] = {"type": "atr", "value": multiple}
        rows.append(
            ConversionRow(
                field=label,
                original=f"{dist.value} {dist.type}",
                converted=f"{multiple} ATR(14)",
                reference_symbol=bars.symbol,
                note=(
                    f"measured against the median ATR(14) of {bars.symbol} "
                    f"{bars.timeframe} over {len(bars):,} bars"
                ),
            )
        )
    if rows:
        out.assumptions.append(
            Assumption(
                id=f"portability:{bars.symbol.lower()}",
                question=(
                    "Your risk is stated in units that differ per instrument. Convert to ATR "
                    f"multiples measured on {bars.symbol} so the same spec runs on any asset?"
                ),
                resolution="; ".join(f"{r.field}: {r.original} -> {r.converted}" for r in rows),
                approved_by=approved_by or "pending",
                alternatives=["keep the original units and run this spec on one instrument only"],
                sensitivity={
                    "if_wrong": (
                        "Stops and targets scale with each instrument's own volatility instead of "
                        "being fixed. On a quiet day the stop is tighter than you intended; on a "
                        "volatile day it is wider."
                    )
                },
            )
        )
    return out, rows


def conversion_table(rows: list[ConversionRow]) -> str:
    if not rows:
        return "Nothing to convert: this spec is already scale-free."
    width = max(16, *(len(r.field) for r in rows))
    lines = [f"  {'field':<{width}} {'original':<17} {'becomes':<24} measured on"]
    for r in rows:
        lines.append(f"  {r.field:<{width}} {r.original:<17} {r.converted:<24} {r.reference_symbol}")
    lines.append("  Nothing has been changed. Approve with: ee-agent spec portable <spec> --approve")
    return "\n".join(lines)
