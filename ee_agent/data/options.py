"""Fetching option chains and option contract candles.

Three sources, same shape:

* **yfinance** -- free, no key. Full chains with bid/ask, volume and open
  interest. This is what makes options reachable at the zero-cost floor.
* **Polygon** -- keyed. Chain snapshots with vendor greeks, plus per-contract
  aggregate bars for backtesting a single contract.
* **synthetic** -- deterministic, offline. Committed fixtures run on this, so
  the option path is tested with no credentials and no network.

Where the vendor supplies greeks, those are used. Where it does not, they are
computed with Black-Scholes and ``Greeks.source`` says so -- a modelled delta
and a vendor delta are not the same claim.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ee_agent.cost.notifier import ledger
from ee_agent.data.bars import BASE_COLUMNS, Bars
from ee_agent.errors import DataError
from ee_agent.instruments.options import (
    ChainRow,
    Greeks,
    OptionChain,
    OptionContract,
    black_scholes,
    implied_volatility,
    option_instrument,
    parse_option,
    third_friday,
)
from ee_agent.secrets.vault import get_secret


# ================================================================ yfinance
def _yfinance_chain(underlying: str, expiries: int = 3) -> OptionChain:
    import yfinance as yf  # optional dependency

    ticker = yf.Ticker(underlying.upper())
    history = ticker.history(period="5d")
    if history is None or len(history) == 0:
        raise DataError(f"yfinance has no price for {underlying}")
    spot = float(history["Close"].iloc[-1])

    available = list(ticker.options or [])
    if not available:
        raise DataError(f"yfinance lists no option expiries for {underlying}")

    chain = OptionChain(
        underlying=underlying.upper(),
        underlying_price=spot,
        as_of=datetime.now(timezone.utc).isoformat(),
        source="yfinance",
    )
    for expiry_text in available[:expiries]:
        frame = ticker.option_chain(expiry_text)
        expiry = date.fromisoformat(expiry_text)
        for side, right in ((frame.calls, "call"), (frame.puts, "put")):
            for _index, row in side.iterrows():
                contract = OptionContract(
                    root=underlying.upper(), expiry=expiry, strike=float(row["strike"]), right=right
                )
                bid = float(row.get("bid", float("nan")) or float("nan"))
                ask = float(row.get("ask", float("nan")) or float("nan"))
                last = float(row.get("lastPrice", float("nan")) or float("nan"))
                vendor_iv = float(row.get("impliedVolatility", float("nan")) or float("nan"))
                chain_row = ChainRow(
                    contract=contract,
                    bid=bid,
                    ask=ask,
                    last=last,
                    volume=float(row.get("volume", 0) or 0),
                    open_interest=float(row.get("openInterest", 0) or 0),
                )
                iv = vendor_iv if vendor_iv == vendor_iv and vendor_iv > 0 else None
                chain_row.greeks = _greeks_for(chain_row, spot, iv, vendor_iv_source="yfinance")
                chain.rows.append(chain_row)
    chain.notes.append(
        "yfinance gives implied volatility but not greeks, so delta/gamma/theta/vega are "
        "Black-Scholes from that IV -- modelled, not quoted."
    )
    return chain


# ================================================================== polygon
def _polygon_chain(underlying: str, expiries: int = 3) -> OptionChain:
    import requests

    key = get_secret("POLYGON_API_KEY")
    if not key:
        raise DataError("POLYGON_API_KEY is not set")

    response = requests.get(
        f"https://api.polygon.io/v3/snapshot/options/{underlying.upper()}",
        params={"limit": 250, "apiKey": key},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results", []) or []
    if not results:
        raise DataError(f"polygon returned no option snapshot for {underlying}")

    spot = 0.0
    for entry in results:
        underlying_asset = entry.get("underlying_asset") or {}
        if underlying_asset.get("price"):
            spot = float(underlying_asset["price"])
            break

    chain = OptionChain(
        underlying=underlying.upper(),
        underlying_price=spot,
        as_of=datetime.now(timezone.utc).isoformat(),
        source="polygon",
    )
    seen_expiries: set[date] = set()
    for entry in results:
        details = entry.get("details") or {}
        try:
            expiry = date.fromisoformat(details["expiration_date"])
        except (KeyError, ValueError):
            continue
        if expiry not in seen_expiries and len(seen_expiries) >= expiries:
            continue
        seen_expiries.add(expiry)
        contract = OptionContract(
            root=underlying.upper(),
            expiry=expiry,
            strike=float(details.get("strike_price", 0.0)),
            right="call" if details.get("contract_type") == "call" else "put",
        )
        quote = entry.get("last_quote") or {}
        day = entry.get("day") or {}
        vendor = entry.get("greeks") or {}
        row = ChainRow(
            contract=contract,
            bid=float(quote.get("bid", float("nan")) or float("nan")),
            ask=float(quote.get("ask", float("nan")) or float("nan")),
            last=float((entry.get("last_trade") or {}).get("price", float("nan")) or float("nan")),
            volume=float(day.get("volume", 0) or 0),
            open_interest=float(entry.get("open_interest", 0) or 0),
        )
        if vendor.get("delta") is not None:
            row.greeks = Greeks(
                price=row.mid,
                delta=float(vendor.get("delta", float("nan"))),
                gamma=float(vendor.get("gamma", float("nan"))),
                theta=float(vendor.get("theta", float("nan"))),
                vega=float(vendor.get("vega", float("nan"))),
                implied_vol=float(entry.get("implied_volatility", float("nan")) or float("nan")),
                source="polygon",
            )
        else:
            row.greeks = _greeks_for(row, spot, entry.get("implied_volatility"), "polygon")
        chain.rows.append(row)
    chain.notes.append("Polygon supplies vendor greeks where available; anything missing is modelled.")
    return chain


def _polygon_option_bars(contract: OptionContract, timeframe: str, start, end) -> Bars:
    import requests

    key = get_secret("POLYGON_API_KEY")
    if not key:
        raise DataError("POLYGON_API_KEY is not set")
    mult, span = {
        "1m": (1, "minute"), "5m": (5, "minute"), "15m": (15, "minute"),
        "1h": (1, "hour"), "1d": (1, "day"),
    }.get(timeframe, (5, "minute"))
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/O:{contract.occ}/range/{mult}/{span}/"
        f"{start.date().isoformat()}/{end.date().isoformat()}"
    )
    rows: list[list] = []
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}
    while url:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        for entry in payload.get("results", []) or []:
            rows.append(
                [
                    pd.to_datetime(entry["t"], unit="ms", utc=True),
                    float(entry["o"]), float(entry["h"]), float(entry["l"]), float(entry["c"]),
                    float(entry.get("v", 0.0)),
                ]
            )
        url = payload.get("next_url")
        params = {"apiKey": key}
    if not rows:
        raise DataError(f"polygon has no {timeframe} bars for {contract.occ}")
    instrument = option_instrument(contract)
    return Bars(
        symbol=contract.occ,
        timeframe=timeframe,
        df=pd.DataFrame(rows, columns=BASE_COLUMNS),
        tz=instrument.timezone,
        source="polygon:options",
    )


# ================================================================ synthetic
def synthetic_chain(
    underlying: str = "SPY",
    spot: float = 580.0,
    expiries: int = 3,
    strikes: int = 9,
    strike_step: float = 5.0,
    base_vol: float = 0.18,
    seed: int = 71,
    as_of: date | None = None,
) -> OptionChain:
    """A deterministic chain with a realistic volatility smile and liquidity.

    Used by the committed fixtures and the acceptance checks, so the whole
    option path -- parsing, greeks, selection, sizing, backtesting -- runs with
    no key and no network.
    """
    rng = np.random.default_rng(seed)
    today = as_of or date.today()
    chain = OptionChain(
        underlying=underlying.upper(),
        underlying_price=spot,
        as_of=datetime.now(timezone.utc).isoformat(),
        source="synthetic",
    )
    centre = round(spot / strike_step) * strike_step
    for expiry in _next_monthly_expiries(today, expiries):
        days = max((expiry - today).days, 1)
        for step in range(-(strikes // 2), strikes // 2 + 1):
            strike = centre + step * strike_step
            if strike <= 0:
                continue
            # Volatility smile: wings are dearer, and short-dated is dearer still.
            moneyness = math.log(strike / spot)
            vol = base_vol + 0.35 * moneyness**2 + 0.04 * math.exp(-days / 30.0)
            for right in ("call", "put"):
                contract = OptionContract(root=underlying.upper(), expiry=expiry, strike=strike, right=right)
                greeks = black_scholes(contract, spot, vol, as_of=today)
                theo = max(greeks.price, 0.01)
                # Spread widens away from the money and on thin contracts.
                half_spread = max(theo * (0.01 + 0.06 * abs(moneyness)), 0.01)
                distance = abs(step)
                open_interest = float(max(0, int(4000 * math.exp(-0.35 * distance) + rng.integers(0, 200))))
                volume = float(max(0, int(open_interest * 0.08 + rng.integers(0, 40))))
                row = ChainRow(
                    contract=contract,
                    bid=round(max(theo - half_spread, 0.01), 2),
                    ask=round(theo + half_spread, 2),
                    last=round(theo, 2),
                    volume=volume,
                    open_interest=open_interest,
                    greeks=greeks,
                )
                chain.rows.append(row)
    chain.notes.append(
        "Synthetic chain: a Black-Scholes surface with a smile and modelled liquidity. "
        "Real enough to exercise selection and sizing, and not real data -- it is never "
        "presented as a market quote."
    )
    return chain


def _next_monthly_expiries(today: date, count: int) -> list[date]:
    """The next ``count`` DISTINCT monthly expiries strictly after ``today``.

    Walking month offsets and clamping forward produced the same third Friday
    twice whenever this month's had already passed, which put duplicate
    contracts in the chain -- a client picking "the 560 call" would have seen
    two of them with different open interest.
    """
    out: list[date] = []
    year, month = today.year, today.month
    while len(out) < count:
        candidate = third_friday(year, month)
        if candidate > today and candidate not in out:
            out.append(candidate)
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


def synthetic_option_bars(
    contract: OptionContract | str,
    underlying_bars: Bars,
    volatility: float = 0.18,
    rate: float = 0.04,
) -> Bars:
    """Price an option bar by bar off real underlying bars.

    This is how a single-contract backtest runs with no option history: every
    bar's premium is repriced from the underlying's OHLC, so the option series
    inherits the underlying's actual path, its gaps and its session structure.
    Theta decays as the expiry approaches, bar by bar.
    """
    if isinstance(contract, str):
        contract = parse_option(contract)
    instrument = option_instrument(contract)
    frame = underlying_bars.df
    rows: list[list] = []
    for i in range(len(underlying_bars)):
        as_of = frame["ts"].iloc[i].date()
        if as_of > contract.expiry:
            break
        opens = black_scholes(contract, float(underlying_bars.open[i]), volatility, as_of, rate).price
        highs = black_scholes(contract, float(underlying_bars.high[i]), volatility, as_of, rate).price
        lows = black_scholes(contract, float(underlying_bars.low[i]), volatility, as_of, rate).price
        closes = black_scholes(contract, float(underlying_bars.close[i]), volatility, as_of, rate).price
        # A put gains as the underlying falls, so high/low invert.
        top, bottom = max(opens, highs, lows, closes), min(opens, highs, lows, closes)
        rows.append(
            [
                frame["ts"].iloc[i],
                round(max(opens, 0.01), 4),
                round(max(top, 0.01), 4),
                round(max(bottom, 0.01), 4),
                round(max(closes, 0.01), 4),
                float(underlying_bars.volume[i]) * 0.002,  # option volume is a fraction of the underlying
            ]
        )
    if not rows:
        raise DataError(
            f"{contract.readable} had already expired before the first bar of "
            f"{underlying_bars.symbol}"
        )
    return Bars(
        symbol=contract.occ,
        timeframe=underlying_bars.timeframe,
        df=pd.DataFrame(rows, columns=BASE_COLUMNS),
        tz=instrument.timezone,
        source=f"repriced-from-{underlying_bars.symbol}",
    )


# ================================================================ resolver
def _greeks_for(row: ChainRow, spot: float, vendor_iv, vendor_iv_source: str) -> Greeks:
    """Vendor IV if given, otherwise solve for it, then model the greeks."""
    contract = row.contract
    iv = None
    if vendor_iv is not None:
        try:
            candidate = float(vendor_iv)
            if candidate == candidate and candidate > 0:
                iv = candidate
        except (TypeError, ValueError):
            iv = None
    if iv is None:
        mid = row.mid
        if mid == mid and mid > 0 and spot > 0:
            solved = implied_volatility(contract, spot, mid)
            iv = solved if solved == solved else 0.2
        else:
            iv = 0.2
    greeks = black_scholes(contract, spot, iv, as_of=date.today())
    greeks.source = f"black-scholes from {vendor_iv_source} IV" if vendor_iv else "black-scholes from solved IV"
    return greeks


def load_chain(
    underlying: str,
    expiries: int = 3,
    prefer: list[str] | None = None,
    fixtures_only: bool = False,
    sink=print,
) -> OptionChain:
    """Load a chain, walking the ladder and reporting each step.

    The ladder is the same idea as the bar resolver: try the best available
    source, fall down on failure, and say which one answered.
    """
    if fixtures_only:
        chain = synthetic_chain(underlying)
        sink(chain.summary())
        return chain

    order = prefer or ["polygon", "yfinance", "synthetic"]
    errors: list[str] = []
    for source in order:
        try:
            if source == "polygon":
                ledger().spend("data_fetch", **{"data.bar_1k_paid": 0.25})
                chain = _polygon_chain(underlying, expiries)
            elif source == "yfinance":
                chain = _yfinance_chain(underlying, expiries)
            elif source == "synthetic":
                chain = synthetic_chain(underlying)
            else:
                continue
            if source != order[0]:
                chain.notes.append(f"fell back to {source} (tried: {', '.join(order[: order.index(source)])})")
            sink(chain.summary())
            return chain
        except Exception as exc:
            errors.append(f"{source}: {exc}")
            continue
    raise DataError(
        f"No source could supply an option chain for {underlying}:\n  " + "\n  ".join(errors)
    )


def load_option_bars(
    contract: OptionContract | str,
    timeframe: str = "5m",
    lookback_days: int = 60,
    underlying_bars: Bars | None = None,
    volatility: float = 0.18,
    fixtures_only: bool = False,
    sink=print,
) -> Bars:
    """Candles for one option contract.

    Real vendor history where a key allows it; otherwise repriced bar by bar
    from the underlying's own candles, which is stated in the source name so a
    result is never mistaken for one built on traded option prices.
    """
    if isinstance(contract, str):
        contract = parse_option(contract)

    if not fixtures_only and get_secret("POLYGON_API_KEY"):
        try:
            end = datetime.now(timezone.utc)
            bars = _polygon_option_bars(contract, timeframe, end - timedelta(days=lookback_days), end)
            sink(bars.age_line())
            return bars
        except Exception as exc:
            sink(f"[options] polygon had no history for {contract.occ} ({exc}); repricing from the underlying")

    if underlying_bars is None:
        from ee_agent.data.loader import load_bars

        underlying_bars = load_bars(
            contract.root, timeframe, lookback_days=lookback_days,
            fixtures_only=fixtures_only, use_cache=not fixtures_only, sink=sink,
        )
    bars = synthetic_option_bars(contract, underlying_bars, volatility=volatility)
    sink(bars.age_line())
    sink(
        f"[options] {contract.readable}: premium repriced from {underlying_bars.symbol} bar by bar "
        f"at {volatility:.0%} vol. These are MODELLED prices, not traded option prices -- a result "
        "built on them is a result about the underlying's path, not about option liquidity."
    )
    return bars
