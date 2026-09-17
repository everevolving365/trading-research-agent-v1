"""The resolver: one entry point for market data, whatever the asset class.

``load_bars("MNQ", "5m")`` and ``load_bars("BTCUSDT", "5m")`` take exactly the
same path through exactly the same code. There is no asset-class branch here --
the instrument registry supplies the differences (hard rule 5).

The ladder is: cache -> preferred source -> next source -> ... -> fixture. Every
step is reported. Every load prints cache age and last bar timestamp (Law One).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from ee_agent.data import sources as _sources  # noqa: F401  (registers fetchers)
from ee_agent.data.bars import Bars
from ee_agent.data.cache import BarCache
from ee_agent.data.integrity import QualityReport, check_and_clean
from ee_agent.data.source_registry import CAPABILITIES, get_fetcher, ladder
from ee_agent.errors import DataError
from ee_agent.instruments.registry import registry
from ee_agent.cost.notifier import ledger

DEFAULT_PREFERENCE = ["databento", "polygon", "binance", "yfinance", "fixture", "csv_ingest"]


def asset_class_of(symbol: str) -> str:
    reg = registry()
    if reg.has(symbol):
        return reg.get(symbol).asset_class
    s = symbol.upper()
    if s.endswith(("USDT", "USDC", "BTC", "ETH")):
        return "crypto"
    if len(s) == 6 and s.isalpha():
        return "forex"
    return "equity"


def load_bars(
    symbol: str,
    timeframe: str = "5m",
    *,
    lookback_days: int = 365,
    start: datetime | None = None,
    end: datetime | None = None,
    prefer: list[str] | None = None,
    use_cache: bool = True,
    max_cache_age_hours: float | None = 24.0,
    fixtures_only: bool = False,
    tz: str | None = None,
    sink: Callable[[str], None] = print,
    clean: bool = True,
) -> Bars:
    end = end or datetime.now(timezone.utc)
    start = start or (end - timedelta(days=lookback_days))
    days_back = max(1, int((end - start).total_seconds() // 86400))
    aclass = asset_class_of(symbol)
    reg = registry()
    local_tz = tz or (reg.get(symbol).timezone if reg.has(symbol) else "America/Chicago")

    chain = ["fixture"] if fixtures_only else ladder(aclass, timeframe, days_back, prefer or DEFAULT_PREFERENCE)
    if not chain:
        raise DataError(
            f"No source serves {symbol} ({aclass}) at {timeframe} going back {days_back} days. "
            f"Ingest a file instead: `ee-agent data ingest <path>`."
        )

    cache = BarCache()
    if use_cache:
        for source in chain:
            hit = cache.get(symbol, timeframe, source, max_age_hours=max_cache_age_hours)
            if hit is not None:
                hit.retimezone(local_tz)
                sink(hit.age_line())
                return hit

    errors: list[str] = []
    for source in chain:
        cap = CAPABILITIES[source]
        try:
            est_bars = _estimate_bars(timeframe, days_back)
            if cap.cost_per_1k_bars_usd > 0:
                ledger().spend(
                    "data_fetch",
                    **{"data.bar_1k_paid": est_bars / 1000.0 * (cap.cost_per_1k_bars_usd / 0.02)},
                )
            fetcher = get_fetcher(source)
            bars = fetcher.fetch(symbol, timeframe, start, end)
            if len(bars) == 0:
                raise DataError(f"{source} returned no bars")
            bars.retimezone(local_tz)
            if clean:
                bars, report = check_and_clean(bars)
                sink(report.summary())
            if use_cache:
                cache.put(bars)
            sink(bars.age_line())
            if len(chain) > 1 and source != chain[0]:
                sink(f"[data] fell back to {source} (tried: {', '.join(chain[: chain.index(source)])})")
            return bars
        except Exception as exc:  # fall down the ladder
            errors.append(f"{source}: {exc}")
            continue

    raise DataError(
        f"Every source failed for {symbol} {timeframe}:\n  " + "\n  ".join(errors)
    )


def load_and_report(symbol: str, timeframe: str = "5m", **kw) -> tuple[Bars, QualityReport]:
    bars = load_bars(symbol, timeframe, clean=False, **kw)
    return check_and_clean(bars)


def _estimate_bars(timeframe: str, days: int) -> int:
    from ee_agent.data.bars import timeframe_minutes

    per_day = (24 * 60) / max(timeframe_minutes(timeframe), 1e-9)
    return int(per_day * days)
