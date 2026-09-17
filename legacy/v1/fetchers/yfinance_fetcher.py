"""
YFinanceFetcher — free fallback for stocks/ETFs when POLYGON_API_KEY is absent.

Limitation: yfinance 1-minute data only covers the most recent 7 days.
Regular market hours only (09:30–16:00 ET) unless explicitly extended.
"""

from __future__ import annotations

import pandas as pd

from .base_fetcher import BaseFetcher


class YFinanceFetcher(BaseFetcher):

    MAX_DAYS = 7

    def fetch(self, symbol: str, days: int) -> pd.DataFrame:
        sym = symbol.upper()

        cached = self.load_from_cache(sym)
        if cached is not None and not cached.empty:
            return cached

        if days > self.MAX_DAYS:
            print(
                f"[YFINANCE] Only 7 days of 1-min data available. "
                "For longer history add POLYGON_API_KEY to .env."
            )
            days = self.MAX_DAYS

        try:
            import yfinance as yf
        except ImportError:
            raise RuntimeError("yfinance is not installed. Run: pip install yfinance")

        period = f"{max(1, min(days, self.MAX_DAYS))}d"
        raw = yf.download(
            sym,
            period=period,
            interval="1m",
            progress=False,
            auto_adjust=False,
            prepost=False,
        )
        if raw is None or raw.empty:
            raise RuntimeError(
                f"yfinance returned no data for {sym}. "
                "Symbol may not exist or market may have been closed."
            )

        # Flatten possible MultiIndex columns (newer yfinance versions)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0] for c in raw.columns]

        df = raw.reset_index().rename(columns={
            "Datetime": "Date",
            "Date": "Date",
            "index": "Date",
        })
        df["Symbol"] = sym
        df = df[["Date", "Symbol", "Open", "High", "Low", "Close", "Volume"]]
        df = self.normalize_frame(df, sym)

        merged = self.merge_into_cache(df, sym)
        if not self.validate_output(merged):
            raise RuntimeError("yfinance output failed universal-format validation.")
        return merged
