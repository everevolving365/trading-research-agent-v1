"""Deterministic bar generation.

Two uses:

1. Building the committed fixtures, so the entire system is testable with zero
   credentials and zero network (Section 2.2).
2. Edge-case generation for Phase 5: gaps, limit moves, holidays, half days,
   rollover days, DST transitions and zero-volume bars.

Everything here is seeded. The same seed produces byte-identical bars, which is
what makes a pinned artifact re-runnable without regeneration.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ee_agent.data.bars import Bars, timeframe_minutes
from ee_agent.instruments.registry import US_HALF_DAYS, US_HOLIDAYS_2024_2026, get_instrument


@dataclass
class GenSpec:
    symbol: str
    timeframe: str = "5m"
    days: int = 252
    start_price: float = 20000.0
    daily_vol: float = 0.011
    seed: int = 7
    session_start: str = "08:00"
    session_end: str = "15:15"
    all_hours: bool = False
    tick_size: float = 0.25
    with_flow: bool = False
    end_date: date | None = None


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def generate(gs: GenSpec) -> Bars:
    """A seeded random walk with intraday structure: an opening drive, a midday
    lull, and a close. Enough shape that session-range strategies behave like
    they do on real data, without pretending to be real data."""
    inst = None
    try:
        inst = get_instrument(gs.symbol)
    except KeyError:
        pass
    tz = ZoneInfo(inst.timezone if inst else "America/Chicago")
    tick = inst.tick_size if inst else gs.tick_size

    rng = np.random.default_rng(gs.seed)
    step_min = timeframe_minutes(gs.timeframe)
    bars_per_day = int((24 * 60) / step_min) if gs.all_hours else int(
        (_minutes(gs.session_end) - _minutes(gs.session_start)) / step_min
    )
    per_bar_vol = gs.daily_vol / np.sqrt(max(bars_per_day, 1))

    end_day = gs.end_date or date(2026, 6, 30)
    days: list[date] = []
    d = end_day
    while len(days) < gs.days:
        iso = d.isoformat()
        tradeable = gs.all_hours or (d.weekday() < 5 and iso not in US_HOLIDAYS_2024_2026)
        if tradeable:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()

    price = gs.start_price
    rows: list[list] = []
    for day in days:
        half = day.isoformat() in US_HALF_DAYS
        start_m = 0 if gs.all_hours else _minutes(gs.session_start)
        end_m = (24 * 60) if gs.all_hours else _minutes(gs.session_end)
        if half and not gs.all_hours:
            end_m = min(end_m, _minutes("12:00"))
        # overnight gap
        price *= float(np.exp(rng.normal(0, gs.daily_vol * 0.35)))
        minute = start_m
        while minute < end_m:
            # intraday volatility shape: open 1.8x, midday 0.6x, close 1.3x
            frac = (minute - start_m) / max(end_m - start_m, 1)
            shape = 1.8 * np.exp(-6 * frac) + 0.6 + 0.7 * np.exp(-8 * (1 - frac))
            sigma = per_bar_vol * shape
            drift = rng.normal(0, sigma)
            o = price
            c = o * float(np.exp(drift))
            wick = abs(rng.normal(0, sigma)) * o
            h = max(o, c) + wick * rng.uniform(0.2, 1.0)
            lo = min(o, c) - wick * rng.uniform(0.2, 1.0)
            vol = float(max(1.0, rng.gamma(shape=3.0, scale=180.0 * shape)))
            local_dt = datetime.combine(day, time(minute // 60 % 24, minute % 60), tzinfo=tz)
            ts = local_dt.astimezone(timezone.utc)
            row = [
                ts,
                _round(o, tick),
                _round(h, tick),
                _round(lo, tick),
                _round(c, tick),
                round(vol, 2),
            ]
            if gs.with_flow:
                imbalance = float(np.clip(rng.normal((c - o) / max(h - lo, tick) * 0.6, 0.25), -0.9, 0.9))
                ask_v = vol * (0.5 + imbalance / 2)
                bid_v = vol - ask_v
                row += [round(bid_v, 2), round(ask_v, 2), int(max(1, vol / 4)), round(ask_v - bid_v, 2)]
            rows.append(row)
            price = c
            minute += int(step_min)

    columns = ["ts", "open", "high", "low", "close", "volume"]
    if gs.with_flow:
        columns += ["bid_volume", "ask_volume", "trades", "delta"]
    df = pd.DataFrame(rows, columns=columns)
    return Bars(
        symbol=gs.symbol,
        timeframe=gs.timeframe,
        df=df,
        tz=inst.timezone if inst else "America/Chicago",
        source="synthetic",
    )


def _round(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 10)


# ----------------------------------------------------------------- edge cases
def inject_gap(bars: Bars, at_index: int, n_missing: int = 12) -> Bars:
    """Remove n bars to create a data hole the integrity engine must catch."""
    df = bars.df.drop(index=range(at_index, min(at_index + n_missing, len(bars.df)))).reset_index(drop=True)
    return Bars(symbol=bars.symbol, timeframe=bars.timeframe, df=df, tz=bars.tz, source=bars.source + ":gapped")


def inject_limit_move(bars: Bars, at_index: int, percent: float = 7.0) -> Bars:
    """A limit move: a single enormous bar, then frozen prices."""
    df = bars.df.copy()
    factor = 1 + percent / 100.0
    df.loc[at_index:, ["open", "high", "low", "close"]] *= factor
    df.loc[at_index, "high"] = df.loc[at_index, "high"] * 1.02
    for j in range(at_index + 1, min(at_index + 10, len(df))):
        df.loc[j, ["open", "high", "low", "close"]] = df.loc[at_index, "close"]
        df.loc[j, "volume"] = 0.0
    return Bars(symbol=bars.symbol, timeframe=bars.timeframe, df=df, tz=bars.tz, source=bars.source + ":limit")


def inject_zero_volume(bars: Bars, at_index: int, n: int = 5) -> Bars:
    df = bars.df.copy()
    df.loc[at_index : at_index + n, "volume"] = 0.0
    return Bars(symbol=bars.symbol, timeframe=bars.timeframe, df=df, tz=bars.tz, source=bars.source + ":zerovol")


def inject_duplicates(bars: Bars, at_index: int, n: int = 3) -> Bars:
    df = bars.df.copy()
    dupes = df.iloc[at_index : at_index + n]
    df = pd.concat([df, dupes]).reset_index(drop=True)
    return Bars(symbol=bars.symbol, timeframe=bars.timeframe, df=df, tz=bars.tz, source=bars.source + ":dupes")


def inject_out_of_order(bars: Bars, at_index: int) -> Bars:
    df = bars.df.copy()
    order = list(range(len(df)))
    order[at_index], order[at_index + 1] = order[at_index + 1], order[at_index]
    df = df.iloc[order].reset_index(drop=True)
    return Bars(symbol=bars.symbol, timeframe=bars.timeframe, df=df, tz=bars.tz, source=bars.source + ":unordered")


def dst_window(symbol: str = "MNQ", timeframe: str = "5m") -> Bars:
    """Bars spanning both US DST transitions."""
    spring = generate(
        GenSpec(symbol=symbol, timeframe=timeframe, days=10, seed=11, end_date=date(2026, 3, 12))
    )
    fall = generate(
        GenSpec(symbol=symbol, timeframe=timeframe, days=10, seed=12, end_date=date(2025, 11, 7))
    )
    df = pd.concat([fall.df, spring.df]).sort_values("ts").reset_index(drop=True)
    return Bars(symbol=symbol, timeframe=timeframe, df=df, tz=fall.tz, source="synthetic:dst")


def holiday_window(symbol: str = "MNQ", timeframe: str = "5m") -> Bars:
    """Bars around Thanksgiving: a full close then a half day."""
    return generate(GenSpec(symbol=symbol, timeframe=timeframe, days=12, seed=13, end_date=date(2025, 12, 2)))


EDGE_CASES = {
    "gap": inject_gap,
    "limit_move": inject_limit_move,
    "zero_volume": inject_zero_volume,
    "duplicates": inject_duplicates,
    "out_of_order": inject_out_of_order,
}
