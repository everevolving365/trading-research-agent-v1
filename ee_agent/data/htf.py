"""Higher-timeframe data on a lower-timeframe chart, the way Pine delivers it.

``request.security(sym, "15", expr)`` with the default ``lookahead_off`` gives a
historical chart bar the value of the most recent 15-minute bar that had
**closed** by the time that chart bar closed. On a 5-minute chart the 09:00-09:15
bar therefore becomes visible on the 09:10 bar (which closes at 09:15), and the
09:00 and 09:05 bars still see the 08:45 bar. Nothing from an unfinished
higher-timeframe bar ever leaks backwards -- which is also why this is causal.

This module is the single definition of that rule. The Python primitives and the
Pine interpreter both call it, so the two cannot quietly disagree about when a
higher-timeframe value arrives.

Supported: intraday minute timeframes at or above the chart's own, that the
chart timeframe divides ("15" on a 1/3/5/15-minute chart). Higher-timeframe bars
are built from the chart's own bars, so a missing chart bar is missing from the
aggregate too -- the same data, not a second source.
"""
from __future__ import annotations

import numpy as np

from ee_agent.data.bars import Bars, _epoch_ns, timeframe_minutes
from ee_agent.errors import PrimitiveError

_MINUTE_NS = 60 * 10**9


def pine_timeframe_minutes(tf: str) -> int:
    """Pine timeframe strings ("15", "60", "15m", "1h") -> minutes.

    Daily and above are refused rather than approximated: TradingView builds a
    futures daily bar from the exchange session (17:00 CT to 16:00 CT), not from
    midnight, and a silent midnight-based daily bar would be a wrong number.
    """
    text = str(tf).strip()
    if not text:
        raise PrimitiveError("empty timeframe")
    if text.isdigit():
        return int(text)
    low = text.lower()
    if low.endswith("m") and low[:-1].isdigit():
        return int(low[:-1])
    if low.endswith("h") and low[:-1].isdigit():
        return int(low[:-1]) * 60
    raise PrimitiveError(
        f"timeframe {tf!r} is not an intraday minute timeframe. Higher-timeframe values are "
        "supported for minute timeframes (\"5\", \"15\", \"60\"); daily bars follow the exchange "
        "session and are not reconstructed from intraday data here."
    )


def _check(bars: Bars, minutes: int) -> int:
    chart = timeframe_minutes(bars.timeframe)
    if minutes < chart:
        raise PrimitiveError(
            f"requested {minutes}m on a {bars.timeframe} chart: that is a LOWER timeframe. "
            "Run the strategy on a chart at or below the requested timeframe."
        )
    if chart <= 0 or (minutes / chart) != int(minutes / chart):
        raise PrimitiveError(
            f"a {bars.timeframe} chart does not divide {minutes}m bars evenly, so the "
            "higher-timeframe bars cannot be rebuilt from the chart's own bars"
        )
    return int(chart)


def htf_bars(bars: Bars, tf: str) -> Bars:
    """The higher-timeframe bars built from ``bars`` (period open time labels)."""
    minutes = pine_timeframe_minutes(tf)
    _check(bars, minutes)
    if minutes == int(timeframe_minutes(bars.timeframe)):
        return bars
    return bars.resample(f"{minutes}m")


def htf_index_map(bars: Bars, htf: Bars, tf: str) -> np.ndarray:
    """For each chart bar, the index of the latest higher-timeframe bar that had
    closed by the chart bar's close, or -1 when none has yet."""
    minutes = pine_timeframe_minutes(tf)
    chart = _check(bars, minutes)
    if len(bars) == 0:
        return np.zeros(0, dtype=np.int64)
    if len(htf) == 0:
        return np.full(len(bars), -1, dtype=np.int64)
    chart_close = _epoch_ns(bars.df["ts"]) + chart * _MINUTE_NS
    htf_end = _epoch_ns(htf.df["ts"]) + minutes * _MINUTE_NS
    return np.searchsorted(htf_end, chart_close, side="right").astype(np.int64) - 1


def htf_view(bars: Bars, tf: str) -> tuple[Bars, np.ndarray]:
    """Both at once: the aggregate and the chart-bar -> HTF-bar map."""
    htf = htf_bars(bars, tf)
    return htf, htf_index_map(bars, htf, tf)
