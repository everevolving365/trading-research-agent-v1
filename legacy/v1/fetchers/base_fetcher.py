"""
BaseFetcher — Abstract base class for every data source.

Enforces the universal data contract:
    Date, Symbol, Open, High, Low, Close, Volume  (1-min bars, sorted ascending)

Every concrete fetcher inherits this and must implement .fetch(symbol, days).
Caching, validation, and CSV I/O are handled here so concrete fetchers
focus only on transport.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import pandas as pd


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


class BaseFetcher(ABC):

    REQUIRED_COLUMNS = ["Date", "Symbol", "Open", "High", "Low", "Close", "Volume"]

    @abstractmethod
    def fetch(self, symbol: str, days: int) -> pd.DataFrame:
        """Fetch 1-minute OHLCV data for `symbol` covering the last `days` days.

        Concrete implementations must return a DataFrame with REQUIRED_COLUMNS
        in that exact order, sorted ascending by Date, no duplicate timestamps,
        and no NaN values in OHLC columns.
        """
        raise NotImplementedError

    # ----- validation ---------------------------------------------------

    def validate_output(self, df: pd.DataFrame) -> bool:
        """Strict check: column presence, dtypes, sort order, no NaNs in OHLC."""
        if df is None or df.empty:
            return False
        if list(df.columns)[: len(self.REQUIRED_COLUMNS)] != self.REQUIRED_COLUMNS:
            return False
        if not pd.api.types.is_datetime64_any_dtype(df["Date"]):
            return False
        if df[["Open", "High", "Low", "Close"]].isna().any().any():
            return False
        if not df["Date"].is_monotonic_increasing:
            return False
        return True

    def normalize_frame(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Coerce dtypes, drop OHLC-NaN rows, fill Volume NaN with 0, sort, dedupe."""
        df = df.copy()
        df["Date"] = pd.to_datetime(df["Date"], utc=True, errors="coerce")
        df["Symbol"] = symbol.upper()

        for col in ("Open", "High", "Low", "Close", "Volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["Open", "High", "Low", "Close", "Date"])
        df["Volume"] = df["Volume"].fillna(0.0)

        df = df.drop_duplicates(subset=["Date"]).sort_values("Date").reset_index(drop=True)
        return df[self.REQUIRED_COLUMNS]

    # ----- cache --------------------------------------------------------

    def _cache_path(self, symbol: str) -> str:
        sym = symbol.upper()
        folder = os.path.join(DATA_DIR, sym)
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{sym}_1min.csv")

    def save_to_cache(self, df: pd.DataFrame, symbol: str) -> str:
        path = self._cache_path(symbol)
        df.to_csv(path, index=False)
        return path

    def load_from_cache(self, symbol: str) -> pd.DataFrame | None:
        path = self._cache_path(symbol)
        if not os.path.exists(path):
            return None
        try:
            df = pd.read_csv(path, parse_dates=["Date"])
            if df.empty:
                return None
            df["Date"] = pd.to_datetime(df["Date"], utc=True, errors="coerce")
            df = df.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)
            print(f"[CACHE HIT] Loaded {len(df):,} rows from local cache for {symbol.upper()}")
            return df
        except Exception as e:
            print(f"[CACHE WARN] Could not read cache for {symbol}: {e}")
            return None

    def merge_into_cache(self, new_df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Append new rows to existing cache, dedupe on Date, sort, persist."""
        existing = self.load_from_cache(symbol)
        if existing is None or existing.empty:
            merged = new_df
        else:
            merged = pd.concat([existing, new_df], ignore_index=True)
            merged = merged.drop_duplicates(subset=["Date"]).sort_values("Date").reset_index(drop=True)
        self.save_to_cache(merged, symbol)
        return merged

    def clear_cache(self, symbol: str) -> bool:
        path = self._cache_path(symbol)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    # ----- helpers ------------------------------------------------------

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def to_unix_ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)
