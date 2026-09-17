"""Volume and order flow as first-class data (ability 24).

Every function here computes from real trade data when the dataset carries
``bid_volume``/``ask_volume``/``delta``, and degrades cleanly to a bar-derived
proxy when it does not. The degradation is always *returned*, never hidden --
Section 9 honest limit 3 says order flow does not exist for most assets, so the
honest thing is to say which one you got.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def _rolling_mean(a: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    if len(a) < n:
        return out
    c = np.cumsum(np.insert(a, 0, 0.0))
    out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


def _rolling_std(a: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    for i in range(n - 1, len(a)):
        out[i] = np.std(a[i - n + 1 : i + 1])
    return out


def signed_volume(bars) -> tuple[np.ndarray, bool]:
    """Per-bar delta. Returns (series, degraded)."""
    delta = getattr(bars, "delta", None)
    if delta is not None and not np.all(np.isnan(delta)):
        return np.nan_to_num(delta), False
    bid = getattr(bars, "bid_volume", None)
    ask = getattr(bars, "ask_volume", None)
    if bid is not None and ask is not None:
        return np.nan_to_num(ask) - np.nan_to_num(bid), False
    # proxy: where the bar closed within its own range, scaled by volume
    rng = np.maximum(bars.high - bars.low, 1e-12)
    position = (bars.close - bars.low) / rng  # 0 = closed on the low, 1 = on the high
    return (2.0 * position - 1.0) * bars.volume, True


def cumulative_delta(bars) -> tuple[np.ndarray, bool]:
    sv, degraded = signed_volume(bars)
    out = np.full(len(bars), np.nan)
    cur_day, acc = None, 0.0
    for i in range(len(bars)):
        if bars.local_date[i] != cur_day:
            cur_day, acc = bars.local_date[i], 0.0
        acc += sv[i]
        out[i] = acc
    return out, degraded


def bid_ask_imbalance(bars) -> tuple[np.ndarray, bool]:
    bid = getattr(bars, "bid_volume", None)
    ask = getattr(bars, "ask_volume", None)
    if bid is None or ask is None:
        sv, degraded = signed_volume(bars)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(bars.volume > 0, sv / bars.volume, 0.0), degraded
    total = np.maximum(np.nan_to_num(bid) + np.nan_to_num(ask), 1e-12)
    return (np.nan_to_num(ask) - np.nan_to_num(bid)) / total, False


def absorption_score(bars, length: int = 20) -> tuple[np.ndarray, bool]:
    """Volume z-score minus range z-score. High = a lot of trade, little travel."""
    rng = np.maximum(bars.high - bars.low, 1e-12)
    vz = (bars.volume - _rolling_mean(bars.volume, length)) / np.maximum(
        _rolling_std(bars.volume, length), 1e-9
    )
    rz = (rng - _rolling_mean(rng, length)) / np.maximum(_rolling_std(rng, length), 1e-9)
    degraded = not bars.has_flow
    return vz - rz, degraded


def trade_size_distribution(bars, buckets: int = 5) -> dict:
    """Distribution of per-bar trade size. Real when ``trades`` is present."""
    trades = getattr(bars, "trades", None)
    if trades is None or np.all(np.isnan(trades)):
        return {"degraded": True, "note": "no trade-count column; use volume per bar as a proxy"}
    with np.errstate(divide="ignore", invalid="ignore"):
        size = np.where(trades > 0, bars.volume / trades, np.nan)
    finite = size[np.isfinite(size)]
    if len(finite) == 0:
        return {"degraded": True, "note": "no finite trade sizes"}
    qs = np.linspace(0, 100, buckets + 1)
    return {
        "degraded": False,
        "quantiles": {f"p{int(q)}": float(np.percentile(finite, q)) for q in qs},
        "mean": float(np.mean(finite)),
    }


@dataclass
class VolumeProfile:
    levels: np.ndarray
    volumes: np.ndarray
    poc: float
    value_area_low: float
    value_area_high: float
    degraded: bool = False

    def to_dict(self) -> dict:
        return {
            "poc": self.poc,
            "value_area_low": self.value_area_low,
            "value_area_high": self.value_area_high,
            "n_levels": int(len(self.levels)),
            "degraded": self.degraded,
        }


def volume_profile(bars, tick_size: float = 0.25, value_area: float = 0.70) -> VolumeProfile:
    """Volume at price with point of control and value area.

    Without tick data, each bar's volume is spread uniformly across the price
    levels it traded through -- the standard, honest approximation.
    """
    if len(bars) == 0:
        return VolumeProfile(np.array([]), np.array([]), float("nan"), float("nan"), float("nan"), True)
    lo, hi = float(np.min(bars.low)), float(np.max(bars.high))
    n_levels = max(1, int(round((hi - lo) / tick_size)) + 1)
    n_levels = min(n_levels, 20000)
    levels = np.linspace(lo, hi, n_levels)
    vols = np.zeros(n_levels)
    step = (hi - lo) / max(n_levels - 1, 1)
    for i in range(len(bars)):
        a = int((bars.low[i] - lo) / step) if step > 0 else 0
        b = int((bars.high[i] - lo) / step) if step > 0 else 0
        a, b = max(0, min(a, n_levels - 1)), max(0, min(b, n_levels - 1))
        span = b - a + 1
        vols[a : b + 1] += bars.volume[i] / span
    poc_idx = int(np.argmax(vols))
    total = vols.sum()
    target = total * value_area
    lo_i = hi_i = poc_idx
    acc = vols[poc_idx]
    while acc < target and (lo_i > 0 or hi_i < n_levels - 1):
        take_low = vols[lo_i - 1] if lo_i > 0 else -1
        take_high = vols[hi_i + 1] if hi_i < n_levels - 1 else -1
        if take_high >= take_low:
            hi_i += 1
            acc += take_high
        else:
            lo_i -= 1
            acc += take_low
    return VolumeProfile(
        levels=levels,
        volumes=vols,
        poc=float(levels[poc_idx]),
        value_area_low=float(levels[lo_i]),
        value_area_high=float(levels[hi_i]),
        degraded=not bars.has_flow,
    )


def vwap_bands(bars, stdevs: tuple[float, ...] = (1.0, 2.0)) -> dict[str, np.ndarray]:
    """Session VWAP with standard deviation bands, computed causally."""
    tp = (bars.high + bars.low + bars.close) / 3.0
    n = len(bars)
    vwap = np.full(n, np.nan)
    var = np.full(n, np.nan)
    cur_day, pv, vv, pv2 = None, 0.0, 0.0, 0.0
    for i in range(n):
        if bars.local_date[i] != cur_day:
            cur_day, pv, vv, pv2 = bars.local_date[i], 0.0, 0.0, 0.0
        pv += tp[i] * bars.volume[i]
        pv2 += (tp[i] ** 2) * bars.volume[i]
        vv += bars.volume[i]
        if vv > 0:
            vwap[i] = pv / vv
            var[i] = max(pv2 / vv - vwap[i] ** 2, 0.0)
    sd = np.sqrt(var)
    out = {"vwap": vwap, "stdev": sd}
    for k in stdevs:
        out[f"upper_{k}"] = vwap + k * sd
        out[f"lower_{k}"] = vwap - k * sd
    return out


@dataclass
class LiquidityMap:
    """Where resting liquidity most likely sits: prior swing extremes and the
    high-volume nodes price keeps returning to."""

    resting_levels: list[float] = field(default_factory=list)
    high_volume_nodes: list[float] = field(default_factory=list)
    degraded: bool = True

    def to_dict(self) -> dict:
        return {
            "resting_levels": [round(x, 6) for x in self.resting_levels],
            "high_volume_nodes": [round(x, 6) for x in self.high_volume_nodes],
            "degraded": self.degraded,
        }


def liquidity_map(bars, lookback: int = 20, top_n: int = 8, tick_size: float = 0.25) -> LiquidityMap:
    if len(bars) < lookback * 2:
        return LiquidityMap()
    highs, lows = bars.high, bars.low
    swing_high, swing_low = [], []
    for i in range(lookback, len(bars) - lookback):
        window_h = highs[i - lookback : i + lookback + 1]
        window_l = lows[i - lookback : i + lookback + 1]
        if highs[i] == window_h.max():
            swing_high.append(float(highs[i]))
        if lows[i] == window_l.min():
            swing_low.append(float(lows[i]))
    prof = volume_profile(bars, tick_size=tick_size)
    order = np.argsort(prof.volumes)[::-1][:top_n] if len(prof.volumes) else []
    nodes = [float(prof.levels[i]) for i in order]
    levels = sorted(set(round(x, 6) for x in (swing_high[-top_n:] + swing_low[-top_n:])))
    return LiquidityMap(resting_levels=levels, high_volume_nodes=sorted(nodes), degraded=not bars.has_flow)


def flow_report(bars) -> dict:
    """One call the tearsheet and the narrator both use."""
    cd, cd_degraded = cumulative_delta(bars)
    imb, imb_degraded = bid_ask_imbalance(bars)
    absorb, ab_degraded = absorption_score(bars)
    prof = volume_profile(bars)
    return {
        "has_real_flow": bool(bars.has_flow),
        "cumulative_delta_last": None if len(cd) == 0 else float(cd[-1]),
        "cumulative_delta_degraded": cd_degraded,
        "imbalance_mean": float(np.nanmean(imb)) if len(imb) else None,
        "imbalance_degraded": imb_degraded,
        "absorption_max": float(np.nanmax(absorb)) if len(absorb) and not np.all(np.isnan(absorb)) else None,
        "absorption_degraded": ab_degraded,
        "volume_profile": prof.to_dict(),
        "trade_size": trade_size_distribution(bars),
        "liquidity": liquidity_map(bars).to_dict(),
        "note": (
            "Order flow computed from real trade data."
            if bars.has_flow
            else "No trade data on this dataset: every flow figure above is a bar-derived proxy."
        ),
    }
