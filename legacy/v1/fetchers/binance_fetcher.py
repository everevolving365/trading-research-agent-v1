"""
BinanceFetcher — public klines endpoint, no API key required.

Endpoint: /api/v3/klines on one of:
  - api.binance.com  (global, geo-blocked from US — returns HTTP 451)
  - api.binance.us   (US-compliant subset, same API)

Auto-failover: on HTTP 451 we move to the next host and remember the
working host for the rest of the session.

Resolution: 1m  |  Max rows per call: 1000  |  Paginate by advancing startTime.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pandas as pd
import requests

from .base_fetcher import BaseFetcher


BINANCE_HOSTS = [
    "https://api.binance.com",
    "https://api.binance.us",
]
KLINES_PATH = "/api/v3/klines"
MAX_LIMIT = 1000
ONE_MIN_MS = 60_000


class BinanceFetcher(BaseFetcher):

    def __init__(self) -> None:
        self._working_host: str | None = None  # cached after first success

    def fetch(self, symbol: str, days: int) -> pd.DataFrame:
        sym = symbol.upper()

        cached = self.load_from_cache(sym)
        end_time = self.utc_now()
        start_time = end_time - timedelta(days=days)

        if cached is not None and not cached.empty:
            cached_end = pd.to_datetime(cached["Date"].max(), utc=True).to_pydatetime()
            if cached_end >= end_time - timedelta(minutes=2):
                return cached
            start_time = cached_end + timedelta(minutes=1)
            print(f"[CACHE EXTEND] Fetching {sym} from {start_time:%Y-%m-%d %H:%M} UTC → now")

        rows = self._fetch_range(sym, self.to_unix_ms(start_time), self.to_unix_ms(end_time))
        if not rows:
            if cached is not None and not cached.empty:
                return cached
            raise RuntimeError(f"No data returned from Binance for {sym}.")

        df = pd.DataFrame(rows, columns=[
            "open_time", "Open", "High", "Low", "Close", "Volume",
            "close_time", "qav", "trades", "tbbav", "tbqav", "ignore",
        ])
        df["Date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["Symbol"] = sym
        df = df[self.REQUIRED_COLUMNS]
        df = self.normalize_frame(df, sym)

        merged = self.merge_into_cache(df, sym)
        if not self.validate_output(merged):
            raise RuntimeError("Binance output failed universal-format validation.")
        return merged

    # ----- internals ----------------------------------------------------

    def _hosts_to_try(self) -> list[str]:
        if self._working_host:
            return [self._working_host] + [h for h in BINANCE_HOSTS if h != self._working_host]
        return list(BINANCE_HOSTS)

    def _fetch_range(self, symbol: str, start_ms: int, end_ms: int) -> list:
        all_rows: list = []
        cursor = start_ms
        retries = 0

        while cursor < end_ms:
            params = {
                "symbol": symbol,
                "interval": "1m",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": MAX_LIMIT,
            }

            resp = None
            geo_blocked_hosts: list[str] = []
            symbol_unknown_hosts: list[str] = []

            for host in self._hosts_to_try():
                url = host + KLINES_PATH
                try:
                    resp = requests.get(url, params=params, timeout=30)
                except requests.RequestException as e:
                    resp = None
                    print(f"[BINANCE] Network error on {host}: {e}")
                    continue

                if resp.status_code == 451:
                    geo_blocked_hosts.append(host)
                    print(f"[BINANCE] {host} is geo-blocked from this region (HTTP 451). "
                          f"Trying next host.")
                    resp = None
                    continue

                if resp.status_code == 429:
                    print(f"[BINANCE] {host} rate limited. Sleeping 60s.")
                    time.sleep(60)
                    # retry same host
                    try:
                        resp = requests.get(url, params=params, timeout=30)
                    except requests.RequestException:
                        resp = None
                        continue

                # Symbol-not-found on this host — try next host before giving up,
                # because Binance.US covers fewer pairs than Binance.com.
                if resp is not None and resp.status_code != 200:
                    try:
                        body = resp.json()
                    except Exception:
                        body = {"msg": resp.text[:200]}
                    if body.get("code") == -1121:
                        symbol_unknown_hosts.append(host)
                        resp = None
                        continue
                    raise RuntimeError(f"Binance error {resp.status_code} on {host}: {body}")

                # 200 OK — remember this host and break out of host loop
                if resp is not None and resp.status_code == 200:
                    self._working_host = host
                    break

            if resp is None or resp.status_code != 200:
                # Every host failed for this request window.
                if geo_blocked_hosts and not symbol_unknown_hosts:
                    raise RuntimeError(
                        "All Binance endpoints are geo-blocked from your location "
                        f"({', '.join(geo_blocked_hosts)}). "
                        "Use a VPN, or switch to a different crypto data source."
                    )
                if symbol_unknown_hosts:
                    raise RuntimeError(
                        f"Symbol '{symbol}' not found on any reachable Binance host "
                        f"({', '.join(symbol_unknown_hosts)}). "
                        "Binance.US has fewer pairs than Binance.com — try a major pair "
                        "like BTCUSDT or BTCUSD."
                    )
                retries += 1
                if retries > 3:
                    raise RuntimeError("Binance request failed after 3 retries (no reachable host).")
                time.sleep(5)
                continue

            batch = resp.json()
            if not batch:
                break
            all_rows.extend(batch)
            last_open = batch[-1][0]
            next_cursor = last_open + ONE_MIN_MS
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            retries = 0
            time.sleep(0.05)  # be polite

        return all_rows
