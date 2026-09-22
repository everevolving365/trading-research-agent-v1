"""Options as a first-class asset class.

"Any asset, any asset class. Explicitly not narrowed to one." Options are the
awkward one: a single underlying explodes into thousands of contracts, each with
its own strike, expiry, multiplier and liquidity, and the thing a strategy keys
off is usually a property of the *chain* rather than one contract's candles.

Hard rule 5 still holds -- there is no option-specific branch anywhere in the
engine. An option contract resolves to an ordinary :class:`Instrument` through
this module, so the backtester, the fill model, the parity harness and the
position ledger all treat it as any other instrument. What is option-specific
lives here and in the chain fetchers, which is exactly the same shape as the
futures rollover logic.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from ee_agent.instruments.registry import Instrument, registry

Right = Literal["call", "put"]

#: OCC-style option symbol: SPY241220C00580000
#: root (1-6) + yymmdd + C|P + strike * 1000, zero-padded to 8
OCC_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<right>[CP])(?P<strike>\d{8})$")

#: Polygon/Databento style: O:SPY241220C00580000
PREFIXED_RE = re.compile(r"^O:(?P<rest>.+)$")

#: Human style the client will actually say: "SPY 580C 20 Dec 24", "SPY 580 call 2024-12-20"
HUMAN_RE = re.compile(
    r"^(?P<root>[A-Za-z]{1,6})\s+(?P<strike>\d+(?:\.\d+)?)\s*(?P<right>[CcPp]|call|put|CALL|PUT)?\s*"
    r"(?P<expiry>\d{4}-\d{2}-\d{2}|\d{6}|\d{1,2}\s+\w{3}\s+\d{2,4})?\s*(?P<right2>call|put|CALL|PUT)?$"
)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True)
class OptionContract:
    """One option contract, parsed from whatever the client typed."""

    root: str
    expiry: date
    strike: float
    right: Right
    multiplier: float = 100.0
    currency: str = "USD"

    @property
    def occ(self) -> str:
        """Canonical OCC symbol -- what the vendors want."""
        strike_part = f"{int(round(self.strike * 1000)):08d}"
        return f"{self.root}{self.expiry:%y%m%d}{'C' if self.right == 'call' else 'P'}{strike_part}"

    @property
    def symbol(self) -> str:
        return self.occ

    @property
    def readable(self) -> str:
        return f"{self.root} {self.strike:g} {self.right} exp {self.expiry.isoformat()}"

    def days_to_expiry(self, as_of: date | None = None) -> int:
        return (self.expiry - (as_of or date.today())).days

    def is_expired(self, as_of: date | None = None) -> bool:
        return self.days_to_expiry(as_of) < 0

    def moneyness(self, underlying_price: float) -> float:
        """Above 1 is in the money for a call, out for a put."""
        if self.strike <= 0:
            return float("nan")
        ratio = underlying_price / self.strike
        return ratio if self.right == "call" else 1.0 / ratio

    def intrinsic(self, underlying_price: float) -> float:
        if self.right == "call":
            return max(underlying_price - self.strike, 0.0) * self.multiplier
        return max(self.strike - underlying_price, 0.0) * self.multiplier

    def to_dict(self) -> dict:
        return {
            "occ": self.occ,
            "root": self.root,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "right": self.right,
            "multiplier": self.multiplier,
        }


def parse_option(text: str) -> OptionContract:
    """Parse an option from OCC, prefixed, or plain-English form.

    The client will type "SPY 580C 2024-12-20" long before they type
    "O:SPY241220C00580000", and refusing the first would be the interface
    getting in the way of the capability.
    """
    raw = text.strip()
    prefixed = PREFIXED_RE.match(raw)
    if prefixed:
        raw = prefixed.group("rest")

    occ = OCC_RE.match(raw.upper().replace(" ", ""))
    if occ:
        return OptionContract(
            root=occ.group("root"),
            expiry=date(2000 + int(occ.group("yy")), int(occ.group("mm")), int(occ.group("dd"))),
            strike=int(occ.group("strike")) / 1000.0,
            right="call" if occ.group("right") == "C" else "put",
        )

    human = HUMAN_RE.match(raw)
    if human:
        right_token = (human.group("right") or human.group("right2") or "").lower()
        if not right_token:
            raise ValueError(
                f"{text!r} does not say whether it is a call or a put. "
                "Try 'SPY 580 call 2024-12-20' or the OCC symbol SPY241220C00580000."
            )
        right: Right = "call" if right_token.startswith("c") else "put"
        expiry_token = human.group("expiry")
        if not expiry_token:
            raise ValueError(
                f"{text!r} has no expiry. Try 'SPY 580 call 2024-12-20', or ask for the chain "
                "with `ee-agent options chain SPY` and pick one."
            )
        return OptionContract(
            root=human.group("root").upper(),
            expiry=_parse_expiry(expiry_token),
            strike=float(human.group("strike")),
            right=right,
        )

    raise ValueError(
        f"I cannot read {text!r} as an option. Accepted: OCC (SPY241220C00580000), "
        "prefixed (O:SPY241220C00580000), or plain (SPY 580 call 2024-12-20)."
    )


def _parse_expiry(token: str) -> date:
    token = token.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
        return date.fromisoformat(token)
    if re.fullmatch(r"\d{6}", token):
        return date(2000 + int(token[:2]), int(token[2:4]), int(token[4:6]))
    match = re.fullmatch(r"(\d{1,2})\s+(\w{3})\w*\s+(\d{2,4})", token)
    if match:
        day, month_name, year = match.groups()
        year_int = int(year)
        if year_int < 100:
            year_int += 2000
        month = MONTHS.get(month_name.lower()[:3])
        if month:
            return date(year_int, month, int(day))
    raise ValueError(f"cannot read {token!r} as an expiry date")


def is_option_symbol(text: str) -> bool:
    try:
        parse_option(text)
        return True
    except ValueError:
        return False


# ============================================================== instrument
#: Per-root contract facts. Index options are cash-settled with different
#: multipliers, and getting that wrong misprices every position by 10x.
ROOT_FACTS: dict[str, dict] = {
    "SPX": {"multiplier": 100.0, "settlement": "cash", "tick_size": 0.05, "timezone": "America/Chicago"},
    "XSP": {"multiplier": 100.0, "settlement": "cash", "tick_size": 0.01, "timezone": "America/Chicago"},
    "NDX": {"multiplier": 100.0, "settlement": "cash", "tick_size": 0.05, "timezone": "America/Chicago"},
    "VIX": {"multiplier": 100.0, "settlement": "cash", "tick_size": 0.05, "timezone": "America/Chicago"},
    "RUT": {"multiplier": 100.0, "settlement": "cash", "tick_size": 0.05, "timezone": "America/Chicago"},
}

DEFAULT_OPTION_FACTS = {
    "multiplier": 100.0,
    "settlement": "physical",
    "tick_size": 0.01,
    "timezone": "America/New_York",
}


def option_instrument(contract: OptionContract | str) -> Instrument:
    """Resolve an option contract to an ordinary Instrument.

    This is the whole trick: once it is an Instrument, nothing downstream knows
    or cares that it is an option. The backtester's tick maths, the fill model,
    the parity harness and the hedge check all work unchanged.
    """
    if isinstance(contract, str):
        contract = parse_option(contract)
    facts = {**DEFAULT_OPTION_FACTS, **ROOT_FACTS.get(contract.root, {})}
    reg = registry()
    symbol = contract.occ

    if symbol in reg._instruments:  # already resolved this session
        return reg._instruments[symbol]

    # Correlation follows the UNDERLYING, so the hedge check sees that a long
    # SPY call and a short ES future are opposing exposure (hard rule 3).
    group = "sp500"
    try:
        group = reg.get(contract.root).correlation_group
    except KeyError:
        group = {"SPX": "sp500", "XSP": "sp500", "NDX": "nasdaq", "RUT": "russell"}.get(
            contract.root, "equity_single"
        )

    instrument = Instrument(
        symbol=symbol,
        name=contract.readable,
        asset_class="option",
        exchange="OPRA",
        currency=contract.currency,
        tick_size=float(facts["tick_size"]),
        # An option's premium is quoted per share; one contract controls
        # `multiplier` shares. tick_value is therefore tick_size * multiplier.
        tick_value=float(facts["tick_size"]) * float(facts["multiplier"]),
        multiplier=float(facts["multiplier"]),
        timezone=str(facts["timezone"]),
        sessions={},
        fees_per_side=0.65,  # typical per-contract retail commission
        margin_day=None,
        margin_overnight=None,
        expiry="dated",
        typical_daily_volume=0.0,
        typical_daily_range_ticks=0.0,
        correlation_group=group,
        prop_eligible=False,
        root=contract.root,
        holiday_calendar="nyse",
        extra={
            "option": contract.to_dict(),
            "settlement": facts["settlement"],
            "underlying": contract.root,
        },
    )
    from ee_agent.instruments.registry import Session

    instrument.sessions = {
        "rth": Session("09:30", "16:00") if facts["settlement"] == "physical" else Session("08:30", "15:00"),
        "eth": Session("09:30", "16:15"),
    }
    reg._instruments[symbol] = instrument
    return instrument


# ================================================================== greeks
@dataclass
class Greeks:
    price: float = float("nan")
    delta: float = float("nan")
    gamma: float = float("nan")
    theta: float = float("nan")
    vega: float = float("nan")
    implied_vol: float = float("nan")
    source: str = "computed"

    def to_dict(self) -> dict:
        return {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in self.__dict__.items()}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_scholes(
    contract: OptionContract,
    underlying: float,
    volatility: float,
    as_of: date | None = None,
    rate: float = 0.04,
    dividend_yield: float = 0.0,
) -> Greeks:
    """Black-Scholes price and greeks.

    Present so a strategy can size by delta and so the engine can value a
    position between quotes. It is a MODEL, not a quote: where the vendor gives
    real greeks, those win, and :func:`Greeks.source` says which you got.
    """
    days = max((contract.expiry - (as_of or date.today())).days, 0)
    time_to_expiry = days / 365.0
    if time_to_expiry <= 0 or volatility <= 0 or underlying <= 0:
        intrinsic = contract.intrinsic(underlying) / contract.multiplier
        return Greeks(
            price=intrinsic,
            delta=(1.0 if intrinsic > 0 else 0.0) * (1 if contract.right == "call" else -1),
            gamma=0.0, theta=0.0, vega=0.0, implied_vol=volatility, source="expired-or-degenerate",
        )

    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (
        math.log(underlying / contract.strike)
        + (rate - dividend_yield + 0.5 * volatility**2) * time_to_expiry
    ) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    discount = math.exp(-rate * time_to_expiry)
    carry = math.exp(-dividend_yield * time_to_expiry)

    if contract.right == "call":
        price = underlying * carry * _norm_cdf(d1) - contract.strike * discount * _norm_cdf(d2)
        delta = carry * _norm_cdf(d1)
        theta = (
            -underlying * carry * _norm_pdf(d1) * volatility / (2 * sqrt_t)
            - rate * contract.strike * discount * _norm_cdf(d2)
            + dividend_yield * underlying * carry * _norm_cdf(d1)
        ) / 365.0
    else:
        price = contract.strike * discount * _norm_cdf(-d2) - underlying * carry * _norm_cdf(-d1)
        delta = -carry * _norm_cdf(-d1)
        theta = (
            -underlying * carry * _norm_pdf(d1) * volatility / (2 * sqrt_t)
            + rate * contract.strike * discount * _norm_cdf(-d2)
            - dividend_yield * underlying * carry * _norm_cdf(-d1)
        ) / 365.0

    return Greeks(
        price=price,
        delta=delta,
        gamma=carry * _norm_pdf(d1) / (underlying * volatility * sqrt_t),
        theta=theta,
        vega=underlying * carry * _norm_pdf(d1) * sqrt_t / 100.0,
        implied_vol=volatility,
        source="black-scholes",
    )


def implied_volatility(
    contract: OptionContract,
    underlying: float,
    market_price: float,
    as_of: date | None = None,
    rate: float = 0.04,
    tolerance: float = 1e-6,
    max_iterations: int = 100,
) -> float:
    """Bisect for IV. Returns NaN rather than a wrong number when the price is
    outside what the model can produce -- an arbitrage-violating quote is data
    to report, not a figure to invent."""
    low, high = 1e-4, 5.0
    price_low = black_scholes(contract, underlying, low, as_of, rate).price
    price_high = black_scholes(contract, underlying, high, as_of, rate).price
    if not (price_low <= market_price <= price_high):
        return float("nan")
    for _ in range(max_iterations):
        mid = 0.5 * (low + high)
        price = black_scholes(contract, underlying, mid, as_of, rate).price
        if abs(price - market_price) < tolerance:
            return mid
        if price < market_price:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


# ============================================================== the chain
@dataclass
class ChainRow:
    contract: OptionContract
    bid: float = float("nan")
    ask: float = float("nan")
    last: float = float("nan")
    volume: float = 0.0
    open_interest: float = 0.0
    greeks: Greeks | None = None

    @property
    def mid(self) -> float:
        if self.bid == self.bid and self.ask == self.ask and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        if mid and mid == mid and self.ask == self.ask and self.bid == self.bid:
            return (self.ask - self.bid) / mid * 100.0
        return float("nan")

    @property
    def tradeable(self) -> bool:
        """Liquidity is the whole game in options. A contract with no open
        interest and a 40% spread is not tradeable at any backtested price."""
        return (
            self.open_interest >= 100
            and self.volume >= 1
            and (self.spread_pct != self.spread_pct or self.spread_pct <= 15.0)
        )

    def to_dict(self) -> dict:
        return {
            **self.contract.to_dict(),
            "bid": _clean(self.bid),
            "ask": _clean(self.ask),
            "mid": _clean(self.mid),
            "volume": self.volume,
            "open_interest": self.open_interest,
            "spread_pct": _clean(self.spread_pct),
            "tradeable": self.tradeable,
            "greeks": self.greeks.to_dict() if self.greeks else None,
        }


@dataclass
class OptionChain:
    underlying: str
    underlying_price: float
    as_of: str
    rows: list[ChainRow] = field(default_factory=list)
    source: str = "unknown"
    notes: list[str] = field(default_factory=list)

    @property
    def expiries(self) -> list[date]:
        return sorted({row.contract.expiry for row in self.rows})

    def for_expiry(self, expiry: date | str) -> list[ChainRow]:
        target = date.fromisoformat(expiry) if isinstance(expiry, str) else expiry
        return [r for r in self.rows if r.contract.expiry == target]

    def nearest_expiry(self, min_days: int = 0) -> date | None:
        today = date.today()
        candidates = [e for e in self.expiries if (e - today).days >= min_days]
        return candidates[0] if candidates else None

    def at_the_money(self, expiry: date | str | None = None, right: Right = "call") -> ChainRow | None:
        rows = self.for_expiry(expiry) if expiry else self.rows
        rows = [r for r in rows if r.contract.right == right]
        if not rows:
            return None
        return min(rows, key=lambda r: abs(r.contract.strike - self.underlying_price))

    def by_delta(self, target: float, expiry: date | str | None = None, right: Right = "call") -> ChainRow | None:
        """Pick the contract nearest a delta -- how traders actually choose."""
        rows = self.for_expiry(expiry) if expiry else self.rows
        rows = [
            r for r in rows
            if r.contract.right == right and r.greeks and r.greeks.delta == r.greeks.delta
        ]
        if not rows:
            return None
        return min(rows, key=lambda r: abs(abs(r.greeks.delta) - abs(target)))

    def tradeable_only(self) -> "OptionChain":
        filtered = [r for r in self.rows if r.tradeable]
        return OptionChain(
            underlying=self.underlying,
            underlying_price=self.underlying_price,
            as_of=self.as_of,
            rows=filtered,
            source=self.source,
            notes=[
                *self.notes,
                f"{len(self.rows) - len(filtered)} of {len(self.rows)} contracts dropped as untradeable "
                "(open interest under 100, no volume, or a spread over 15% of mid)",
            ],
        )

    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "underlying_price": self.underlying_price,
            "as_of": self.as_of,
            "source": self.source,
            "n_contracts": len(self.rows),
            "expiries": [e.isoformat() for e in self.expiries],
            "notes": self.notes,
        }

    def summary(self, limit: int = 8) -> str:
        lines = [
            f"[chain] {self.underlying} at {self.underlying_price:,.2f} -- {len(self.rows)} contract(s) "
            f"across {len(self.expiries)} expiry(ies), from {self.source}"
        ]
        expiry = self.nearest_expiry()
        if expiry:
            rows = sorted(self.for_expiry(expiry), key=lambda r: (r.contract.right, r.contract.strike))
            lines.append(f"        nearest expiry {expiry.isoformat()}:")
            lines.append(
                f"        {'strike':>9} {'right':<5} {'bid':>8} {'ask':>8} {'delta':>7} "
                f"{'OI':>8} {'spread':>8}  tradeable"
            )
            for row in rows[:limit]:
                delta = row.greeks.delta if row.greeks else float("nan")
                lines.append(
                    f"        {row.contract.strike:>9,.2f} {row.contract.right:<5} {row.bid:>8.2f} "
                    f"{row.ask:>8.2f} {delta:>7.3f} {row.open_interest:>8,.0f} "
                    f"{row.spread_pct:>7.1f}%  {'yes' if row.tradeable else 'NO'}"
                )
            if len(rows) > limit:
                lines.append(f"        ... and {len(rows) - limit} more at this expiry")
        for note in self.notes:
            lines.append(f"        {note}")
        return "\n".join(lines)


def _clean(value: float):
    return None if isinstance(value, float) and math.isnan(value) else round(value, 6)


def third_friday(year: int, month: int) -> date:
    """Standard monthly expiry."""
    day = date(year, month, 15)
    while day.weekday() != 4:
        day += timedelta(days=1)
    return day
