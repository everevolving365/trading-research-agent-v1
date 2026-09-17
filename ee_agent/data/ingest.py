"""Universal ingestion (ability 18).

Any file in any format -- CSV, JSON, parquet, broker export, pasted text --
inferred, normalised and registered. The client never states a schema.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ee_agent.data.bars import BASE_COLUMNS, Bars
from ee_agent.data.integrity import check_and_clean
from ee_agent.errors import DataError
from ee_agent.instruments.registry import registry, synthetic_instrument

# Column aliases seen in the wild: exchange dumps, broker exports, TradingView
# exports, NinjaTrader, Sierra Chart, MT4/5, generic spreadsheets.
ALIASES: dict[str, list[str]] = {
    "ts": [
        "ts", "time", "timestamp", "datetime", "date", "date_time", "bar_time", "opentime",
        "open_time", "start", "start_time", "t", "index", "local time", "gmt time",
    ],
    "open": ["open", "o", "openprice", "open_price", "first", "px_open"],
    "high": ["high", "h", "highprice", "high_price", "max", "px_high"],
    "low": ["low", "l", "lowprice", "low_price", "min", "px_low"],
    "close": ["close", "c", "closeprice", "close_price", "last", "px_last", "settle"],
    "volume": ["volume", "v", "vol", "qty", "quantity", "size", "contracts", "shares", "totalvolume"],
    "bid_volume": ["bid_volume", "bidvolume", "bidvol", "sell_volume", "sellvol", "askvolume_sell"],
    "ask_volume": ["ask_volume", "askvolume", "askvol", "buy_volume", "buyvol"],
    "trades": ["trades", "num_trades", "number_of_trades", "count", "ticks"],
    "delta": ["delta", "cum_delta", "cumulativedelta", "net_volume"],
}

FILL_ALIASES: dict[str, list[str]] = {
    "ts": ["time", "timestamp", "datetime", "fill_time", "filled_at", "date", "execution_time"],
    "symbol": ["symbol", "instrument", "contract", "ticker", "asset", "market"],
    "side": ["side", "direction", "action", "b/s", "buy_sell", "type"],
    "qty": ["qty", "quantity", "size", "contracts", "shares", "filled_qty", "volume"],
    "price": ["price", "fill_price", "avg_price", "average_price", "executed_price"],
    "fee": ["fee", "commission", "fees", "cost"],
    "pnl": ["pnl", "profit", "realized_pnl", "net_pnl", "p/l", "gross_pnl"],
}


@dataclass
class IngestResult:
    bars: Bars | None
    fills: pd.DataFrame | None
    kind: str
    mapping: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = f"[ingest] recognised as {self.kind}"
        if self.bars is not None:
            head += f": {len(self.bars):,} bars of {self.bars.symbol} {self.bars.timeframe}"
        if self.fills is not None:
            head += f": {len(self.fills):,} fills"
        mapped = ", ".join(f"{v}->{k}" for k, v in self.mapping.items())
        return head + (f"\n         columns {mapped}" if mapped else "")


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _map_columns(columns, aliases: dict[str, list[str]]) -> dict[str, str]:
    lookup = {_normalise(c): c for c in columns}
    mapping: dict[str, str] = {}
    for canonical, options in aliases.items():
        for option in options:
            key = _normalise(option)
            if key in lookup:
                mapping[canonical] = lookup[key]
                break
    return mapping


def _read_any(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in (".parquet", ".pq"):
        return pd.read_parquet(path)
    if suffix in (".json", ".ndjson", ".jsonl"):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("["):
            return pd.DataFrame(json.loads(text))
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        return pd.DataFrame(rows)
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path)
    # csv / tsv / txt / unknown: sniff the delimiter
    sample = path.read_text(encoding="utf-8", errors="replace")[:8192]
    delim = max([",", ";", "\t", "|"], key=sample.count)
    return pd.read_csv(path, sep=delim, engine="python")


def parse_timestamps(series: pd.Series) -> pd.Series:
    """Handle epoch seconds, epoch millis, ISO, and localised strings."""
    if pd.api.types.is_numeric_dtype(series):
        v = float(pd.to_numeric(series, errors="coerce").dropna().iloc[0])
        unit = "s" if v < 1e11 else ("ms" if v < 1e14 else "us")
        return pd.to_datetime(series, unit=unit, utc=True)
    out = pd.to_datetime(series, utc=True, errors="coerce", format="mixed")
    if out.isna().mean() > 0.5:
        out = pd.to_datetime(series, utc=True, errors="coerce", dayfirst=True)
    return out


def infer_timeframe(ts: pd.Series) -> str:
    if len(ts) < 3:
        return "1m"
    deltas = ts.diff().dropna().dt.total_seconds()
    step = float(deltas.mode().iloc[0]) if len(deltas.mode()) else float(deltas.median())
    table = [(1, "1s"), (60, "1m"), (300, "5m"), (900, "15m"), (1800, "30m"), (3600, "1h"),
             (14400, "4h"), (86400, "1d")]
    best = min(table, key=lambda t: abs(t[0] - step))
    return best[1] if step > 0 else "1m"


def ingest_dataframe(
    df: pd.DataFrame, symbol: str | None = None, timeframe: str | None = None, tz: str | None = None
) -> IngestResult:
    bar_map = _map_columns(df.columns, ALIASES)
    fill_map = _map_columns(df.columns, FILL_ALIASES)
    has_ohlc = all(k in bar_map for k in ("open", "high", "low", "close"))
    looks_like_fills = {"price", "qty", "side"}.issubset(fill_map.keys()) and not has_ohlc

    if looks_like_fills:
        out = pd.DataFrame(
            {
                "ts": parse_timestamps(df[fill_map["ts"]]) if "ts" in fill_map else pd.NaT,
                "symbol": df[fill_map["symbol"]] if "symbol" in fill_map else (symbol or "UNKNOWN"),
                "side": df[fill_map["side"]].astype(str).str.lower().str.strip(),
                "qty": pd.to_numeric(df[fill_map["qty"]], errors="coerce"),
                "price": pd.to_numeric(df[fill_map["price"]], errors="coerce"),
            }
        )
        for extra in ("fee", "pnl"):
            if extra in fill_map:
                out[extra] = pd.to_numeric(df[fill_map[extra]], errors="coerce")
        out["side"] = out["side"].replace(
            {"b": "buy", "s": "sell", "bot": "buy", "sld": "sell", "long": "buy", "short": "sell"}
        )
        return IngestResult(None, out.dropna(subset=["price"]), "broker fill history", fill_map)

    if not has_ohlc:
        if "close" in bar_map:  # close-only series: synthesise flat bars
            ts = parse_timestamps(df[bar_map["ts"]]) if "ts" in bar_map else None
            close = pd.to_numeric(df[bar_map["close"]], errors="coerce")
            out = pd.DataFrame(
                {"ts": ts, "open": close, "high": close, "low": close, "close": close,
                 "volume": pd.to_numeric(df[bar_map.get("volume", bar_map["close"])], errors="coerce").fillna(0)}
            )
            notes = ["close-only series: open/high/low synthesised from close; "
                     "intrabar stop and target fills are unavailable on this dataset"]
        else:
            raise DataError(
                f"Could not recognise this file. Columns seen: {list(df.columns)[:12]}. "
                "Needs at least a timestamp and a close, or a broker fill export."
            )
    else:
        ts = parse_timestamps(df[bar_map["ts"]]) if "ts" in bar_map else None
        if ts is None:
            raise DataError("No timestamp column found; cannot order the bars.")
        out = pd.DataFrame({"ts": ts})
        for col in ("open", "high", "low", "close"):
            out[col] = pd.to_numeric(df[bar_map[col]], errors="coerce")
        out["volume"] = (
            pd.to_numeric(df[bar_map["volume"]], errors="coerce").fillna(0.0) if "volume" in bar_map else 0.0
        )
        for col in ("bid_volume", "ask_volume", "trades", "delta"):
            if col in bar_map:
                out[col] = pd.to_numeric(df[bar_map[col]], errors="coerce")
        notes = []

    out = out.dropna(subset=["ts", "close"]).reset_index(drop=True)
    if len(out) == 0:
        raise DataError("No usable rows after parsing.")
    sym = (symbol or "INGESTED").upper()
    tf = timeframe or infer_timeframe(out["ts"])
    reg = registry()
    if not reg.has(sym):
        synthetic_instrument(sym, asset_class="equity", tick_size=0.01, tick_value=0.01)
        notes.append(
            f"{sym} was not in the instrument registry: registered with placeholder tick size 0.01. "
            "Add a real entry to instruments.yaml before trading it."
        )
    bars = Bars(
        symbol=sym,
        timeframe=tf,
        df=out,
        tz=tz or (reg.get(sym).timezone if reg.has(sym) else "UTC"),
        source="ingest",
        fetched_at=datetime.now(timezone.utc),
    )
    bars, report = check_and_clean(bars)
    notes.append(report.summary())
    return IngestResult(bars, None, "OHLCV bars", bar_map, notes)


def ingest_file(path: str | Path, symbol: str | None = None, timeframe: str | None = None) -> Bars:
    p = Path(path)
    if not p.exists():
        raise DataError(f"No such file: {p}")
    df = _read_any(p)
    guess = symbol or re.split(r"[_\-. ]", p.stem)[0].upper()
    result = ingest_dataframe(df, symbol=guess, timeframe=timeframe)
    if result.bars is None:
        raise DataError(f"{p.name} looks like {result.kind}, not bar data. Use ingest_any() instead.")
    return result.bars


def ingest_any(path: str | Path, symbol: str | None = None) -> IngestResult:
    p = Path(path)
    return ingest_dataframe(_read_any(p), symbol=symbol or re.split(r"[_\-. ]", p.stem)[0].upper())


def ingest_text(text: str, symbol: str | None = None) -> IngestResult:
    """Pasted text: the client drops a table into the conversation."""
    cleaned = text.strip()
    if not cleaned:
        raise DataError("Nothing pasted.")
    if cleaned[0] in "[{":
        data = json.loads(cleaned)
        df = pd.DataFrame(data if isinstance(data, list) else [data])
    else:
        delim = max([",", ";", "\t", "|"], key=cleaned[:4096].count)
        df = pd.read_csv(io.StringIO(cleaned), sep=delim, engine="python")
    return ingest_dataframe(df, symbol=symbol)
