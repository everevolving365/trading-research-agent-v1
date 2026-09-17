"""The data integrity engine (ability 20) -- Law One's first line of defence.

Gap detection, duplicate bars, out-of-order timestamps, timezone normalisation,
DST handling, split and dividend adjustment, continuous futures stitching, and a
quality score on every dataset. Nothing downstream trusts a dataset that has not
passed through here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ee_agent.data.bars import Bars, timeframe_minutes


@dataclass
class QualityReport:
    symbol: str
    timeframe: str
    n_bars: int
    score: float
    duplicates_removed: int = 0
    reordered: int = 0
    gaps: list[tuple[str, str, int]] = field(default_factory=list)
    zero_volume_bars: int = 0
    inverted_bars: int = 0
    dst_transitions: list[str] = field(default_factory=list)
    stitched_contracts: list[str] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def missing_bar_estimate(self) -> int:
        return sum(g[2] for g in self.gaps)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "n_bars": self.n_bars,
            "score": round(self.score, 4),
            "duplicates_removed": self.duplicates_removed,
            "reordered": self.reordered,
            "n_gaps": len(self.gaps),
            "missing_bars_estimate": self.missing_bar_estimate,
            "zero_volume_bars": self.zero_volume_bars,
            "inverted_bars": self.inverted_bars,
            "dst_transitions": self.dst_transitions,
            "stitched_contracts": self.stitched_contracts,
            "adjustments": self.adjustments,
            "notes": self.notes,
        }

    def summary(self) -> str:
        lines = [
            f"[quality] {self.symbol} {self.timeframe}: score {self.score:.2f} over {self.n_bars:,} bars"
        ]
        if self.duplicates_removed:
            lines.append(f"          {self.duplicates_removed} duplicate bar(s) removed")
        if self.reordered:
            lines.append(f"          {self.reordered} out-of-order timestamp(s) sorted")
        if self.gaps:
            worst = sorted(self.gaps, key=lambda g: -g[2])[:3]
            lines.append(
                f"          {len(self.gaps)} gap(s), ~{self.missing_bar_estimate:,} missing bars; "
                f"worst: {', '.join(f'{a}->{b} ({n})' for a, b, n in worst)}"
            )
        if self.zero_volume_bars:
            lines.append(f"          {self.zero_volume_bars} zero-volume bar(s)")
        if self.inverted_bars:
            lines.append(f"          {self.inverted_bars} bar(s) with high < low or close outside range")
        for note in self.notes:
            lines.append(f"          {note}")
        return "\n".join(lines)


def _expected_step_minutes(bars: Bars) -> float:
    return timeframe_minutes(bars.timeframe)


def check_and_clean(
    bars: Bars,
    *,
    session_aware: bool = True,
    max_gap_multiple: float = 1.5,
) -> tuple[Bars, QualityReport]:
    """Clean the dataset in place-ish and score it. Always returns both."""
    df = bars.df
    n_before = len(df)

    # ---- out of order -----------------------------------------------------
    ts = df["ts"].to_numpy()
    reordered = int(np.sum(ts[1:] < ts[:-1])) if len(ts) > 1 else 0
    if reordered:
        df = df.sort_values("ts", kind="stable")

    # ---- duplicates -------------------------------------------------------
    dup_mask = df.duplicated(subset=["ts"], keep="last")
    duplicates = int(dup_mask.sum())
    if duplicates:
        df = df[~dup_mask]

    df = df.reset_index(drop=True)
    cleaned = Bars(
        symbol=bars.symbol,
        timeframe=bars.timeframe,
        df=df,
        tz=bars.tz,
        source=bars.source,
        fetched_at=bars.fetched_at,
    )

    report = QualityReport(
        symbol=bars.symbol,
        timeframe=bars.timeframe,
        n_bars=len(cleaned),
        score=1.0,
        duplicates_removed=duplicates,
        reordered=reordered,
    )

    # ---- gaps -------------------------------------------------------------
    step = _expected_step_minutes(cleaned)
    if len(cleaned) > 1 and step > 0:
        deltas = np.diff(cleaned.df["ts"].astype("int64").to_numpy()) / 60e9  # minutes
        threshold = step * max_gap_multiple
        for i in np.flatnonzero(deltas > threshold):
            a = cleaned.df["ts"].iloc[i]
            b = cleaned.df["ts"].iloc[i + 1]
            missing = int(deltas[i] / step) - 1
            if session_aware and _is_session_boundary(cleaned, int(i)):
                continue
            report.gaps.append((a.isoformat(), b.isoformat(), missing))

    # ---- bar sanity -------------------------------------------------------
    inverted = (
        (cleaned.high < cleaned.low)
        | (cleaned.close > cleaned.high + 1e-9)
        | (cleaned.close < cleaned.low - 1e-9)
        | (cleaned.open > cleaned.high + 1e-9)
        | (cleaned.open < cleaned.low - 1e-9)
    )
    report.inverted_bars = int(np.sum(inverted))
    report.zero_volume_bars = int(np.sum(cleaned.volume <= 0))

    # ---- DST --------------------------------------------------------------
    report.dst_transitions = _dst_transitions(cleaned)
    if report.dst_transitions:
        report.notes.append(
            f"{len(report.dst_transitions)} DST transition(s) in range; session windows are "
            f"evaluated in {cleaned.tz} local time, so the wall clock is correct on both sides."
        )

    # ---- score ------------------------------------------------------------
    n = max(len(cleaned), 1)
    penalty = 0.0
    penalty += min(0.30, report.missing_bar_estimate / n)
    penalty += min(0.20, duplicates / n * 5)
    penalty += min(0.10, reordered / n * 5)
    penalty += min(0.20, report.inverted_bars / n * 10)
    penalty += min(0.10, report.zero_volume_bars / n * 0.5)
    report.score = round(max(0.0, 1.0 - penalty), 4)
    cleaned.quality_score = report.score
    cleaned.quality_notes = [report.summary()]
    if n_before != len(cleaned):
        report.notes.append(f"{n_before - len(cleaned)} row(s) dropped during cleaning")
    return cleaned, report


def _is_session_boundary(bars: Bars, i: int) -> bool:
    """A gap that spans a date change or a weekend is a closed market, not a hole."""
    a_date, b_date = bars.local_date[i], bars.local_date[i + 1]
    if a_date != b_date:
        return True
    return False


def _dst_transitions(bars: Bars) -> list[str]:
    if len(bars) == 0:
        return []
    local = bars.df["ts"].dt.tz_convert(bars.tz)
    offsets = local.map(lambda t: t.utcoffset().total_seconds())
    changes = np.flatnonzero(np.diff(offsets.to_numpy()) != 0)
    return [local.iloc[int(i) + 1].isoformat() for i in changes]


# ------------------------------------------------------- corporate actions
def adjust_splits_dividends(
    bars: Bars, splits: list[tuple[str, float]] | None = None, dividends: list[tuple[str, float]] | None = None
) -> tuple[Bars, list[str]]:
    """Back-adjust prices for splits and dividends.

    Applied to every bar strictly BEFORE the action date, which is the only
    adjustment that does not leak future information into a backtest.
    """
    notes: list[str] = []
    df = bars.df.copy()
    for day, ratio in sorted(splits or [], key=lambda x: x[0]):
        mask = df["ts"] < pd.Timestamp(day, tz="UTC")
        if mask.any() and ratio not in (0, 1):
            for col in ("open", "high", "low", "close"):
                df.loc[mask, col] = df.loc[mask, col] / ratio
            df.loc[mask, "volume"] = df.loc[mask, "volume"] * ratio
            notes.append(f"split {ratio}:1 on {day} back-adjusted over {int(mask.sum())} bars")
    for day, amount in sorted(dividends or [], key=lambda x: x[0]):
        mask = df["ts"] < pd.Timestamp(day, tz="UTC")
        if mask.any() and amount:
            for col in ("open", "high", "low", "close"):
                df.loc[mask, col] = df.loc[mask, col] - amount
            notes.append(f"dividend {amount} on {day} back-adjusted over {int(mask.sum())} bars")
    out = Bars(
        symbol=bars.symbol,
        timeframe=bars.timeframe,
        df=df,
        tz=bars.tz,
        source=bars.source,
        fetched_at=bars.fetched_at,
        quality_score=bars.quality_score,
        quality_notes=list(bars.quality_notes),
    )
    return out, notes


def stitch_continuous(
    contracts: list[tuple[str, Bars]],
    method: str = "ratio",
) -> tuple[Bars, list[str]]:
    """Continuous futures contract stitching.

    ``contracts`` is ordered oldest first as ``(label, bars)``. Each roll point
    is the boundary between consecutive contracts; the older leg is adjusted so
    the price series is continuous across the roll.

    ``ratio`` (default) multiplies the old leg by new/old at the roll, which
    keeps percentage returns intact -- the right choice when a strategy measures
    distance in ATR or percent. ``difference`` keeps point distances intact,
    which is the right choice for a fixed-tick stop.
    """
    if not contracts:
        raise ValueError("stitch_continuous needs at least one contract")
    notes: list[str] = []
    frames: list[pd.DataFrame] = []
    label0, first = contracts[0]
    acc = first.df.copy()
    frames.append(acc)
    for (prev_label, prev_bars), (label, nxt) in zip(contracts, contracts[1:]):
        roll_ts = nxt.df["ts"].iloc[0]
        overlap_prev = prev_bars.df[prev_bars.df["ts"] <= roll_ts]
        if len(overlap_prev) == 0:
            frames.append(nxt.df.copy())
            continue
        old_px = float(overlap_prev["close"].iloc[-1])
        new_px = float(nxt.df["close"].iloc[0])
        if method == "ratio" and old_px:
            factor = new_px / old_px
            for frame in frames:
                for col in ("open", "high", "low", "close"):
                    frame[col] = frame[col] * factor
            notes.append(f"roll {prev_label}->{label} at {roll_ts.isoformat()}: ratio x{factor:.6f}")
        else:
            offset = new_px - old_px
            for frame in frames:
                for col in ("open", "high", "low", "close"):
                    frame[col] = frame[col] + offset
            notes.append(f"roll {prev_label}->{label} at {roll_ts.isoformat()}: offset {offset:+.4f}")
        frames.append(nxt.df.copy())

    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts"], keep="last")
    out = Bars(
        symbol=contracts[0][1].symbol,
        timeframe=contracts[0][1].timeframe,
        df=df,
        tz=contracts[0][1].tz,
        source=f"{contracts[0][1].source}:continuous",
    )
    out.quality_notes = notes
    return out, notes
