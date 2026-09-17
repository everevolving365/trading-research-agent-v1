"""
DatabentoFetcher — US futures via Databento's GLBX.MDP3 dataset (CME products).

Schema: ohlcv-1m
Default symbology: continuous front-month (e.g. ES.c.0, NQ.c.0).
"""

from __future__ import annotations

import os
from datetime import timedelta

import pandas as pd

from .base_fetcher import BaseFetcher


DEFAULT_DATASET = "GLBX.MDP3"


class DatabentoFetcher(BaseFetcher):

    def __init__(self, api_key: str | None = None, dataset: str = DEFAULT_DATASET):
        self.api_key = api_key or os.getenv("DATABENTO_API_KEY", "").strip()
        self.dataset = dataset
        if not self.api_key:
            raise RuntimeError(
                "Futures data requires a Databento API key. "
                "Sign up at https://databento.com. "
                "Add DATABENTO_API_KEY to your .env file."
            )

    def fetch(self, symbol: str, days: int) -> pd.DataFrame:
        sym = symbol.upper()
        cache_key = sym.split(".")[0]  # ES.c.0 -> ES

        cached = self.load_from_cache(cache_key)
        end_time = self.utc_now()
        start_time = end_time - timedelta(days=days)

        if cached is not None and not cached.empty:
            cached_end = pd.to_datetime(cached["Date"].max(), utc=True).to_pydatetime()
            if cached_end >= end_time - timedelta(minutes=2):
                return cached
            start_time = cached_end + timedelta(minutes=1)
            print(f"[CACHE EXTEND] Fetching {sym} from {start_time:%Y-%m-%d %H:%M} UTC → now")

        try:
            import databento as db
        except ImportError:
            raise RuntimeError("databento package not installed. Run: pip install databento")

        # Continuous contract default if user gave plain root.
        databento_symbol = sym if "." in sym else f"{sym}.c.0"

        client = db.Historical(self.api_key)
        try:
            store = client.timeseries.get_range(
                dataset=self.dataset,
                schema="ohlcv-1m",
                symbols=[databento_symbol],
                stype_in="continuous" if ".c." in databento_symbol else "raw_symbol",
                start=start_time.strftime("%Y-%m-%dT%H:%M:%S"),
                end=end_time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
        except Exception as e:
            msg = str(e).lower()
            if "insufficient" in msg or "credits" in msg or "balance" in msg:
                raise RuntimeError(
                    "Your Databento account has insufficient credits. "
                    "Log in at https://databento.com to add credits."
                )
            if "not found" in msg or "unknown symbol" in msg or "no symbols" in msg:
                raise RuntimeError(
                    f"Symbol '{sym}' not found in Databento's {self.dataset} dataset. "
                    "Common futures: ES (S&P 500), NQ (Nasdaq), CL (Crude Oil), "
                    "GC (Gold), SI (Silver), ZB (30Y Bond), ZN (10Y Note)."
                )
            raise RuntimeError(f"Databento error: {e}")

        df = store.to_df()
        if df is None or df.empty:
            if cached is not None and not cached.empty:
                return cached
            raise RuntimeError(f"Databento returned no rows for {sym} in this range.")

        # Databento prices for GLBX.MDP3 come as fixed-precision; .to_df() typically
        # converts to floats already. Defensive scale only if values look like raw ticks.
        for col in ("open", "high", "low", "close"):
            if col in df.columns and df[col].abs().max() > 1e7:
                df[col] = df[col] * 1e-9

        df = df.reset_index().rename(columns={
            "ts_event": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        })
        df["Symbol"] = cache_key
        df = df[self.REQUIRED_COLUMNS]
        df = self.normalize_frame(df, cache_key)

        merged = self.merge_into_cache(df, cache_key)
        if not self.validate_output(merged):
            raise RuntimeError("Databento output failed universal-format validation.")
        return merged
