"""
Router — given a classified asset, return the right fetcher instance.

Routing map (per brief):
  STOCK   -> polygon  (fallback: yfinance if no POLYGON_API_KEY)
  ETF     -> polygon  (fallback: yfinance)
  INDEX   -> polygon  (fallback: yfinance)
  FOREX   -> polygon  (no fallback)
  CRYPTO  -> binance  (no key needed)
  FUTURES -> databento (no free fallback)
"""

from __future__ import annotations

import os

from fetchers.base_fetcher import BaseFetcher
from fetchers.binance_fetcher import BinanceFetcher
from fetchers.polygon_fetcher import PolygonFetcher
from fetchers.yfinance_fetcher import YFinanceFetcher
from fetchers.databento_fetcher import DatabentoFetcher


ROUTING_MAP = {
    "STOCK": "polygon",
    "ETF": "polygon",
    "INDEX": "polygon",
    "FOREX": "polygon",
    "CRYPTO": "binance",
    "FUTURES": "databento",
}


class RoutingError(RuntimeError):
    pass


def has_polygon_key() -> bool:
    return bool(os.getenv("POLYGON_API_KEY", "").strip())


def has_databento_key() -> bool:
    return bool(os.getenv("DATABENTO_API_KEY", "").strip())


def get_fetcher(classified: dict) -> tuple[BaseFetcher, str]:
    """Returns (fetcher_instance, source_label)."""
    asset_type = classified["classified_type"]
    target = ROUTING_MAP.get(asset_type)

    if target is None or asset_type == "UNKNOWN":
        raise RoutingError(
            f"Could not classify '{classified['original_input']}' into a supported asset type. "
            "Try a more explicit name (e.g. AAPL, BTCUSDT, ES futures, EUR/USD, SPY)."
        )

    if target == "binance":
        return BinanceFetcher(), "binance"

    if target == "polygon":
        if has_polygon_key():
            return PolygonFetcher(), "polygon"
        # FOREX has no free fallback
        if asset_type == "FOREX":
            raise RoutingError(
                "Forex requires a Polygon REST API key. "
                "Add POLYGON_API_KEY to .env (https://polygon.io/dashboard/api-keys)."
            )
        print(f"[ROUTER] POLYGON_API_KEY missing — falling back to yfinance for {asset_type}.")
        return YFinanceFetcher(), "yfinance"

    if target == "databento":
        if not has_databento_key():
            raise RoutingError(
                "Futures data requires a Databento API key. "
                "Visit https://databento.com to subscribe. "
                "Add your key to .env as DATABENTO_API_KEY."
            )
        return DatabentoFetcher(), "databento"

    raise RoutingError(f"No fetcher configured for target '{target}'.")


def availability_report() -> dict:
    """Snapshot used by main.py to print the welcome screen."""
    return {
        "CRYPTO":  ("Binance", True, "no key required"),
        "STOCK":   ("Polygon.io" if has_polygon_key() else "yfinance (fallback, 7d max)",
                    True, "key loaded" if has_polygon_key() else "POLYGON_API_KEY not set — fallback active"),
        "ETF":     ("Polygon.io" if has_polygon_key() else "yfinance (fallback, 7d max)",
                    True, "key loaded" if has_polygon_key() else "POLYGON_API_KEY not set — fallback active"),
        "INDEX":   ("Polygon.io" if has_polygon_key() else "yfinance (fallback, 7d max)",
                    True, "key loaded" if has_polygon_key() else "POLYGON_API_KEY not set — fallback active"),
        "FOREX":   ("Polygon.io", has_polygon_key(),
                    "key loaded" if has_polygon_key() else "POLYGON_API_KEY required"),
        "FUTURES": ("Databento", has_databento_key(),
                    "key loaded" if has_databento_key() else "DATABENTO_API_KEY not set"),
    }
