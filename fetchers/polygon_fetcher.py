"""
PolygonFetcher — stocks, ETFs, indices, forex via Polygon.io REST API.

Endpoint: /v2/aggs/ticker/{symbol}/range/1/minute/{from}/{to}
Symbol formats:
  - Stocks/ETFs:  AAPL, SPY
  - Forex:        C:EURUSD
  - Indices:      I:SPX
"""

from __future__ import annotations

import os
import time
from datetime import timedelta

import pandas as pd
import requests

from .base_fetcher import BaseFetcher


POLYGON_BASE = "https://api.polygon.io"
MAX_LIMIT = 50_000


class PolygonFetcher(BaseFetcher):

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("POLYGON_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError(
                "POLYGON_API_KEY missing. Get a REST key at "
                "https://polygon.io/dashboard/api-keys and add it to .env."
            )

    def fetch(self, symbol: str, days: int) -> pd.DataFrame:
        sym = symbol.upper()
        cache_key = self._cache_key(sym)

        cached = self.load_from_cache(cache_key)
        end_time = self.utc_now()
        start_time = end_time - timedelta(days=days)

        if cached is not None and not cached.empty:
            cached_end = pd.to_datetime(cached["Date"].max(), utc=True).to_pydatetime()
            if cached_end >= end_time - timedelta(minutes=2):
                return cached
            start_time = cached_end + timedelta(minutes=1)
            print(f"[CACHE EXTEND] Fetching {sym} from {start_time:%Y-%m-%d %H:%M} UTC → now")

        rows = self._fetch_range(sym, start_time, end_time)
        if not rows:
            if cached is not None and not cached.empty:
                return cached
            raise RuntimeError(
                f"No data found for {sym} in this date range. "
                "The market may have been closed or the symbol may not exist on Polygon."
            )

        df = pd.DataFrame(rows)
        df = df.rename(columns={
            "t": "Date",
            "o": "Open",
            "h": "High",
            "l": "Low",
            "c": "Close",
            "v": "Volume",
        })
        df["Date"] = pd.to_datetime(df["Date"], unit="ms", utc=True)
        df["Symbol"] = sym
        df = df[self.REQUIRED_COLUMNS]
        df = self.normalize_frame(df, cache_key)

        merged = self.merge_into_cache(df, cache_key)
        if not self.validate_output(merged):
            raise RuntimeError("Polygon output failed universal-format validation.")
        return merged

    # ----- internals ----------------------------------------------------

    @staticmethod
    def _cache_key(symbol: str) -> str:
        # Strip prefix delimiters so the cache filename is clean.
        # C:EURUSD -> EURUSD ; I:SPX -> SPX
        return symbol.split(":")[-1].upper()

    def _fetch_range(self, symbol: str, start_dt, end_dt) -> list:
        url = (
            f"{POLYGON_BASE}/v2/aggs/ticker/{symbol}/range/1/minute/"
            f"{start_dt.strftime('%Y-%m-%d')}/{end_dt.strftime('%Y-%m-%d')}"
        )
        params = {
            "adjusted": "true",
            "sort": "asc",
            "limit": MAX_LIMIT,
            "apiKey": self.api_key,
        }

        all_rows: list = []
        next_url: str | None = url
        first = True

        while next_url:
            try:
                if first:
                    resp = requests.get(next_url, params=params, timeout=60)
                    first = False
                else:
                    # next_url already includes its cursor params
                    resp = requests.get(next_url, params={"apiKey": self.api_key}, timeout=60)
            except requests.RequestException as e:
                raise RuntimeError(f"Polygon network error: {e}")

            if resp.status_code == 403:
                raise RuntimeError(
                    "Polygon 403: API key invalid or tier doesn't support 1-min aggregates."
                )
            if resp.status_code == 429:
                print("[POLYGON] Rate limited. Sleeping 60s.")
                time.sleep(60)
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"Polygon error {resp.status_code}: {resp.text[:300]}")

            body = resp.json()
            results = body.get("results") or []
            all_rows.extend(results)

            next_url = body.get("next_url")
            if next_url:
                time.sleep(0.2)  # gentle pacing for paginated calls

        return all_rows
