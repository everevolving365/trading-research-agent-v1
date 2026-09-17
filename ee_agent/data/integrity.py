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
    short_sessions: list[tuple[str, int, int]] = field(default_factory=list)
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
            "short_sessions": [
                {"day": day, "bars": actual, "expected": expected}
                for day, actual, expected in self.short_sessions
            ],
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
        if self.short_sessions:
            worst = sorted(self.short_sessions, key=lambda s: s[1] - s[2])[:3]
            lines.append(
                f"          {len(self.short_sessions)} short session(s); worst: "
                + ", ".join(f"{day} had {actual} of ~{expected} bars" for day, actual, expected in worst)
            )
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
    # Bars sorts on construction and records how many inversions it fixed;
    # anything still out of order here was introduced after that.
    ts = df["ts"].to_numpy()
    reordered = int(np.sum(ts[1:] < ts[:-1])) if len(ts) > 1 else 0
    reordered += int(getattr(bars, "reordered_on_load", 0))
    if ts.size > 1 and np.any(ts[1:] < ts[:-1]):
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
        # NOTE: never convert timestamps by dividing a raw int64 view. pandas
        # stores datetime64 at whatever resolution it chose (ns, us, ...), so
        # that silently scales every gap by 1000x on some versions and finds
        # nothing. total_seconds() is resolution-independent.
        deltas = cleaned.df["ts"].diff().dt.total_seconds().to_numpy()[1:] / 60.0  # minutes
        threshold = step * max_gap_multiple
        for i in np.flatnonzero(deltas > threshold):
            a = cleaned.df["ts"].iloc[i]
            b = cleaned.df["ts"].iloc[i + 1]
            missing = int(deltas[i] / step) - 1
            if session_aware and _is_session_boundary(cleaned, int(i)):
                continue
            report.gaps.append((a.isoformat(), b.isoformat(), missing))

    # ---- short sessions ---------------------------------------------------
    # A jump that lands on the next trading day is forgiven as an overnight
    # break, so a session that simply STOPS halfway through would otherwise
    # score a clean 1.00. Compare every session against the typical one.
    report.short_sessions = _short_sessions(cleaned)

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
    if report.short_sessions:
        missing = sum(expected - actual for _day, actual, expected in report.short_sessions)
        penalty += min(0.30, missing / n)
    report.score = round(max(0.0, 1.0 - penalty), 4)
    cleaned.quality_score = report.score
    cleaned.quality_notes = [report.summary()]
    if n_before != len(cleaned):
        report.notes.append(f"{n_before - len(cleaned)} row(s) dropped during cleaning")
    return cleaned, report


def _is_session_boundary(bars: Bars, i: int) -> bool:
    """Is this jump a closed market, or a hole?

    An overnight or weekend break is not missing data. An entire missing
    *trading session* is, and forgiving every jump that crosses midnight would
    hide exactly that -- a dataset with a whole day absent would score a clean
    1.00.
    """
    from datetime import date as _date, timedelta

    a_date, b_date = str(bars.local_date[i]), str(bars.local_date[i + 1])
    if a_date == b_date:
        return False
    try:
        start, end = _date.fromisoformat(a_date), _date.fromisoformat(b_date)
    except ValueError:  # pragma: no cover - non-ISO calendar labels
        return True

    instrument = None
    try:
        from ee_agent.instruments.registry import registry

        if registry().has(bars.symbol):
            instrument = registry().get(bars.symbol)
    except Exception:  # pragma: no cover - registry unavailable
        instrument = None

    def trades_on(day: _date) -> bool:
        if instrument is not None:
            return instrument.is_trading_day(day)
        return day.weekday() < 5

    skipped = []
    cursor = start + timedelta(days=1)
    while cursor < end:
        if trades_on(cursor):
            skipped.append(cursor)
        cursor += timedelta(days=1)
    # No trading day was skipped: this is just the market being shut.
    return not skipped


def _short_sessions(bars: Bars, threshold: float = 0.7, min_sessions: int = 4) -> list[tuple[str, int, int]]:
    """Sessions holding materially fewer bars than a typical one.

    The first and last sessions are skipped: a dataset almost always starts and
    ends mid-session, and reporting that as a defect would cry wolf on every
    clean file.
    """
    import collections

    if len(bars) == 0:
        return []
    counts = collections.Counter(str(d) for d in bars.local_date)
    days = sorted(counts)
    if len(days) < min_sessions:
        return []
    interior = days[1:-1]
    if not interior:
        return []
    typical = int(np.median([counts[d] for d in interior]))
    if typical <= 0:
        return []
    out: list[tuple[str, int, int]] = []
    for day in interior:
        if counts[day] < typical * threshold:
            out.append((day, counts[day], typical))
    return out


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
