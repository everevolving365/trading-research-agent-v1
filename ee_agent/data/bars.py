"""The one bar container every module speaks.

Columns are fixed, timestamps are always tz-aware UTC, and the session-local
calendar fields every primitive needs (local date, minute of day, weekday) are
precomputed once so no primitive ever re-derives them per bar.

Order-flow columns are optional and absent by default -- the flow layer degrades
to bar-derived proxies rather than failing (Section 9, honest limit 3).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

BASE_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
FLOW_COLUMNS = ["bid_volume", "ask_volume", "trades", "delta"]


@dataclass
class Bars:
    """Immutable-by-convention OHLCV series with session-local calendar fields."""

    symbol: str
    timeframe: str
    df: pd.DataFrame
    tz: str = "America/Chicago"
    source: str = "unknown"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    quality_score: float = 1.0
    quality_notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- building
    def __post_init__(self) -> None:
        missing = [c for c in BASE_COLUMNS if c not in self.df.columns]
        if missing:
            raise ValueError(f"Bars for {self.symbol} missing columns: {missing}")
        df = self.df.reset_index(drop=True).copy()
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.sort_values("ts").reset_index(drop=True)
        self.df = df
        self._recompute_calendar()

    def _recompute_calendar(self) -> None:
        local = self.df["ts"].dt.tz_convert(self.tz)
        self._local = local
        self.local_date = local.dt.strftime("%Y-%m-%d").to_numpy()
        self.minute_of_day = (local.dt.hour * 60 + local.dt.minute).to_numpy(dtype=np.int32)
        self.weekday = local.dt.weekday.to_numpy(dtype=np.int8)
        self.ts = self.df["ts"].to_numpy()
        self.open = self.df["open"].to_numpy(dtype=float)
        self.high = self.df["high"].to_numpy(dtype=float)
        self.low = self.df["low"].to_numpy(dtype=float)
        self.close = self.df["close"].to_numpy(dtype=float)
        self.volume = self.df["volume"].to_numpy(dtype=float)
        for col in FLOW_COLUMNS:
            setattr(self, col, self.df[col].to_numpy(dtype=float) if col in self.df.columns else None)

    @classmethod
    def from_rows(
        cls,
        symbol: str,
        timeframe: str,
        rows: Iterable[Sequence],
        tz: str = "America/Chicago",
        source: str = "unknown",
        columns: Sequence[str] | None = None,
    ) -> "Bars":
        df = pd.DataFrame(list(rows), columns=list(columns or BASE_COLUMNS))
        return cls(symbol=symbol, timeframe=timeframe, df=df, tz=tz, source=source)

    # --------------------------------------------------------------- access
    def __len__(self) -> int:
        return len(self.df)

    @property
    def has_flow(self) -> bool:
        return getattr(self, "delta", None) is not None or getattr(self, "bid_volume", None) is not None

    def slice(self, start: int, end: int) -> "Bars":
        out = Bars(
            symbol=self.symbol,
            timeframe=self.timeframe,
            df=self.df.iloc[start:end].copy(),
            tz=self.tz,
            source=self.source,
            fetched_at=self.fetched_at,
            quality_score=self.quality_score,
            quality_notes=list(self.quality_notes),
        )
        return out

    def between(self, start: datetime, end: datetime) -> "Bars":
        mask = (self.df["ts"] >= pd.Timestamp(start)) & (self.df["ts"] < pd.Timestamp(end))
        idx = np.flatnonzero(mask.to_numpy())
        if len(idx) == 0:
            return self.slice(0, 0)
        return self.slice(int(idx[0]), int(idx[-1]) + 1)

    def retimezone(self, tz: str) -> "Bars":
        self.tz = tz
        self._recompute_calendar()
        return self

    # ------------------------------------------------------------ identity
    @property
    def content_hash(self) -> str:
        """Pins a dataset so a pinned artifact is re-runnable without regeneration."""
        arr = np.ascontiguousarray(
            np.column_stack(
                [
                    # normalise to nanoseconds first: pandas may store us or ns
                    # depending on version, and a pinned artifact must hash the
                    # same on every machine.
                    self.df["ts"].astype("datetime64[ns]").astype("int64").to_numpy(),
                    self.open,
                    self.high,
                    self.low,
                    self.close,
                    self.volume,
                ]
            )
        )
        h = hashlib.sha256(arr.tobytes())
        h.update(f"{self.symbol}|{self.timeframe}|{self.tz}".encode())
        return "sha256:" + h.hexdigest()

    @property
    def last_bar_time(self) -> datetime | None:
        if len(self.df) == 0:
            return None
        return self.df["ts"].iloc[-1].to_pydatetime()

    @property
    def first_bar_time(self) -> datetime | None:
        if len(self.df) == 0:
            return None
        return self.df["ts"].iloc[0].to_pydatetime()

    def age_line(self) -> str:
        """Cache age and last bar timestamp, printed on EVERY load. Law One:
        stale data is never silent."""
        now = datetime.now(timezone.utc)
        age_s = (now - self.fetched_at).total_seconds()
        last = self.last_bar_time
        lag = (now - last).total_seconds() if last else float("nan")
        return (
            f"[data] {self.symbol} {self.timeframe} | {len(self):,} bars | source={self.source} | "
            f"cache age {_human(age_s)} | last bar {last.isoformat() if last else 'n/a'} "
            f"({_human(lag)} old) | quality {self.quality_score:.2f}"
        )

    def resample(self, timeframe: str) -> "Bars":
        """Multi-resolution: any higher timeframe is reconstructed from the
        lowest available (ability 22)."""
        rule = _pandas_rule(timeframe)
        df = self.df.set_index("ts")
        agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        for col in FLOW_COLUMNS:
            if col in df.columns:
                agg[col] = "sum"
        out = df.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["open"]).reset_index()
        return Bars(
            symbol=self.symbol,
            timeframe=timeframe,
            df=out,
            tz=self.tz,
            source=f"{self.source}:resampled",
            fetched_at=self.fetched_at,
            quality_score=self.quality_score,
            quality_notes=list(self.quality_notes),
        )


def _pandas_rule(timeframe: str) -> str:
    tf = timeframe.strip().lower()
    unit = tf[-1]
    n = tf[:-1] or "1"
    return {"s": f"{n}s", "m": f"{n}min", "h": f"{n}h", "d": f"{n}D"}.get(unit, f"{n}min")


def timeframe_minutes(timeframe: str) -> float:
    tf = timeframe.strip().lower()
    unit, n = tf[-1], float(tf[:-1] or 1)
    return {"s": n / 60.0, "m": n, "h": n * 60, "d": n * 60 * 24}.get(unit, n)


def _human(seconds: float) -> str:
    if seconds != seconds:  # NaN
        return "unknown"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds/60:.0f}m"
    if seconds < 172800:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"
