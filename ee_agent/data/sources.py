"""Fetchers. One class per source, all returning the same :class:`Bars`.

Every paid source is built against recorded fixture responses committed to
``tests/fixtures/`` (Section 2.2). The fetchers are complete and tested; they
simply have no key until a client supplies one. When a key IS present they hit
the real endpoint; when it is absent and a fixture exists they serve the
fixture and say so, loudly, in the source name.
"""
from __future__ import annotations

import csv
import json
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ee_agent.data.bars import BASE_COLUMNS, Bars
from ee_agent.data.source_registry import CAPABILITIES, register_fetcher
from ee_agent.errors import DataError
from ee_agent.paths import fixtures_dir
from ee_agent.secrets.vault import get_secret


class Fetcher(ABC):
    name: str = "abstract"

    @property
    def capability(self):
        return CAPABILITIES[self.name]

    @abstractmethod
    def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Bars: ...

    # ---- fixtures ------------------------------------------------------
    def fixture_path(self, symbol: str, timeframe: str) -> Path:
        return fixtures_dir() / "data" / f"{self.name}_{symbol.upper()}_{timeframe}.csv"

    def has_fixture(self, symbol: str, timeframe: str) -> bool:
        return self.fixture_path(symbol, timeframe).exists()

    def load_fixture(self, symbol: str, timeframe: str) -> Bars:
        path = self.fixture_path(symbol, timeframe)
        if not path.exists():
            raise DataError(
                f"No fixture for {symbol} {timeframe} from {self.name} at {path.name}. "
                f"Record one with `ee-agent data record --source {self.name} --symbol {symbol}`."
            )
        df = pd.read_csv(path)
        return Bars(
            symbol=symbol.upper(),
            timeframe=timeframe,
            df=df,
            source=f"{self.name}:fixture",
            fetched_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
        )


class FixtureFetcher(Fetcher):
    """Serves whatever has been recorded, for any source. The build's default."""

    name = "fixture"

    def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Bars:
        direct = fixtures_dir() / "data" / f"fixture_{symbol.upper()}_{timeframe}.csv"
        if direct.exists():
            df = pd.read_csv(direct)
            bars = Bars(symbol=symbol.upper(), timeframe=timeframe, df=df, source="fixture")
        else:
            for other in ("binance", "databento", "polygon", "yfinance"):
                candidate = fixtures_dir() / "data" / f"{other}_{symbol.upper()}_{timeframe}.csv"
                if candidate.exists():
                    df = pd.read_csv(candidate)
                    bars = Bars(symbol=symbol.upper(), timeframe=timeframe, df=df, source=f"fixture:{other}")
                    break
            else:
                raise DataError(f"No fixture for {symbol} {timeframe}")
        if not (start and end):
            return bars
        window = bars.between(start, end)
        if len(window) == 0:
            # The committed fixtures age. A request for "the last 60 days" will
            # eventually fall entirely outside them, and returning nothing would
            # read as "no data exists" rather than "your window has moved past
            # the recording". Hand back what the fixture holds -- age_line()
            # prints exactly how stale it is, so this is never silent.
            return bars
        return window


class CsvIngestFetcher(Fetcher):
    """Reads whatever the client already has on disk (see ``ee_agent.data.ingest``)."""

    name = "csv_ingest"

    def __init__(self, path: Path | None = None):
        self.path = path

    def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Bars:
        from ee_agent.data.ingest import ingest_file

        if not self.path:
            raise DataError("csv_ingest needs a file path")
        return ingest_file(self.path, symbol=symbol, timeframe=timeframe)


class BinanceFetcher(Fetcher):
    """Keyless public klines. This is the zero-cost floor's data source."""

    name = "binance"
    BASE = "https://api.binance.com/api/v3/klines"
    INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1d": "1d"}

    def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Bars:
        interval = self.INTERVALS.get(timeframe)
        if interval is None:
            raise DataError(f"binance has no {timeframe} interval")
        try:
            import requests
        except ImportError:  # pragma: no cover
            return self.load_fixture(symbol, timeframe)

        rows: list[list] = []
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        try:
            while cursor < end_ms:
                resp = requests.get(
                    self.BASE,
                    params={
                        "symbol": symbol.upper(),
                        "interval": interval,
                        "startTime": cursor,
                        "endTime": end_ms,
                        "limit": 1000,
                    },
                    timeout=20,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                for k in batch:
                    rows.append(
                        [
                            pd.to_datetime(k[0], unit="ms", utc=True),
                            float(k[1]),
                            float(k[2]),
                            float(k[3]),
                            float(k[4]),
                            float(k[5]),
                            float(k[9]),  # taker buy base volume -> ask side
                            float(k[5]) - float(k[9]),
                            float(k[8]),  # number of trades
                        ]
                    )
                cursor = int(batch[-1][0]) + 1
                if len(batch) < 1000:
                    break
        except Exception as exc:  # network down, rate limited, offline build
            if self.has_fixture(symbol, timeframe):
                return self.load_fixture(symbol, timeframe).between(start, end)
            raise DataError(f"binance fetch failed and no fixture exists: {exc}") from exc

        df = pd.DataFrame(
            rows, columns=[*BASE_COLUMNS, "ask_volume", "bid_volume", "trades"]
        )
        df["delta"] = df["ask_volume"] - df["bid_volume"]
        return Bars(symbol=symbol.upper(), timeframe=timeframe, df=df, tz="UTC", source="binance")


class YFinanceFetcher(Fetcher):
    name = "yfinance"

    def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Bars:
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            return self.load_fixture(symbol, timeframe).between(start, end)
        interval = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m", "1d": "1d"}.get(timeframe, "5m")
        try:
            raw = yf.download(
                symbol, start=start, end=end, interval=interval, progress=False, auto_adjust=False
            )
            if raw is None or len(raw) == 0:
                raise DataError("yfinance returned nothing")
            df = raw.reset_index()
            df.columns = [str(c[0] if isinstance(c, tuple) else c).lower() for c in df.columns]
            tcol = "datetime" if "datetime" in df.columns else "date"
            out = pd.DataFrame(
                {
                    "ts": pd.to_datetime(df[tcol], utc=True),
                    "open": df["open"].astype(float),
                    "high": df["high"].astype(float),
                    "low": df["low"].astype(float),
                    "close": df["close"].astype(float),
                    "volume": df["volume"].astype(float),
                }
            )
            return Bars(symbol=symbol.upper(), timeframe=timeframe, df=out, source="yfinance")
        except Exception as exc:
            if self.has_fixture(symbol, timeframe):
                return self.load_fixture(symbol, timeframe).between(start, end)
            raise DataError(f"yfinance fetch failed and no fixture exists: {exc}") from exc


class PolygonFetcher(Fetcher):
    name = "polygon"
    BASE = "https://api.polygon.io/v2/aggs/ticker"

    def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Bars:
        key = get_secret("POLYGON_API_KEY")
        if not key:
            return self.load_fixture(symbol, timeframe).between(start, end)
        import requests

        mult, span = {"1m": (1, "minute"), "5m": (5, "minute"), "15m": (15, "minute"),
                      "1h": (1, "hour"), "1d": (1, "day")}.get(timeframe, (5, "minute"))
        url = (
            f"{self.BASE}/{symbol.upper()}/range/{mult}/{span}/"
            f"{start.date().isoformat()}/{end.date().isoformat()}"
        )
        rows: list[list] = []
        params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}
        try:
            while url:
                resp = requests.get(url, params=params, timeout=30)
                resp.raise_for_status()
                payload = resp.json()
                for r in payload.get("results", []) or []:
                    rows.append(
                        [
                            pd.to_datetime(r["t"], unit="ms", utc=True),
                            float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]),
                            float(r.get("v", 0.0)), float(r.get("n", 0.0)),
                        ]
                    )
                url = payload.get("next_url")
                params = {"apiKey": key}
        except Exception as exc:
            if self.has_fixture(symbol, timeframe):
                return self.load_fixture(symbol, timeframe).between(start, end)
            raise DataError(f"polygon fetch failed: {exc}") from exc
        df = pd.DataFrame(rows, columns=[*BASE_COLUMNS, "trades"])
        return Bars(symbol=symbol.upper(), timeframe=timeframe, df=df, source="polygon")


class DatabentoFetcher(Fetcher):
    """CME futures, the only source here carrying real order flow."""

    name = "databento"

    def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Bars:
        key = get_secret("DATABENTO_API_KEY")
        if not key:
            return self.load_fixture(symbol, timeframe).between(start, end)
        try:
            import databento as db  # type: ignore

            client = db.Historical(key)
            schema = {"1s": "ohlcv-1s", "1m": "ohlcv-1m", "1h": "ohlcv-1h", "1d": "ohlcv-1d"}.get(
                timeframe, "ohlcv-1m"
            )
            data = client.timeseries.get_range(
                dataset="GLBX.MDP3",
                symbols=[symbol.upper()],
                stype_in="continuous",
                schema=schema,
                start=start,
                end=end,
            )
            df = data.to_df().reset_index()
            out = pd.DataFrame(
                {
                    "ts": pd.to_datetime(df["ts_event"], utc=True),
                    "open": df["open"].astype(float),
                    "high": df["high"].astype(float),
                    "low": df["low"].astype(float),
                    "close": df["close"].astype(float),
                    "volume": df["volume"].astype(float),
                }
            )
            bars = Bars(symbol=symbol.upper(), timeframe=timeframe, df=out, source="databento")
            if timeframe != schema.split("-")[-1]:
                bars = bars.resample(timeframe)
            return bars
        except Exception as exc:
            if self.has_fixture(symbol, timeframe):
                return self.load_fixture(symbol, timeframe).between(start, end)
            raise DataError(f"databento fetch failed: {exc}") from exc


register_fetcher("fixture", FixtureFetcher)
register_fetcher("csv_ingest", CsvIngestFetcher)
register_fetcher("binance", BinanceFetcher)
register_fetcher("yfinance", YFinanceFetcher)
register_fetcher("polygon", PolygonFetcher)
register_fetcher("databento", DatabentoFetcher)


def record_fixture(source: str, symbol: str, timeframe: str, bars: Bars) -> Path:
    """Write a recorded response so this code path is testable without a key."""
    path = fixtures_dir() / "data" / f"{source}_{symbol.upper()}_{timeframe}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df = bars.df.copy()
    df["ts"] = df["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    df.to_csv(path, index=False)
    meta = path.with_suffix(".json")
    meta.write_text(
        json.dumps(
            {
                "source": source,
                "symbol": symbol.upper(),
                "timeframe": timeframe,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "n_bars": len(bars),
                "content_hash": bars.content_hash,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
