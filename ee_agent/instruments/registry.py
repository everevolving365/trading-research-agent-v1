"""The instrument registry: the only place asset behaviour is allowed to live.

Hard rule 5. Everything downstream -- the engine, the fill model, the sizing,
the hedge check, the Pine emission, the prop rule packs -- reads its numbers
from here. That is what makes the Phase 2 acceptance criterion possible: the
same spec, unedited, runs on MNQ and on BTCUSDT and is correctly scaled on both.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REGISTRY_FILE = Path(__file__).with_name("instruments.yaml")

# US market holidays that matter for the session calendar and the edge-case test
# generator (Phase 5). Dates are exchange closures or early closes.
US_HOLIDAYS_2024_2026: dict[str, str] = {
    "2024-01-01": "New Year", "2024-01-15": "MLK", "2024-02-19": "Presidents",
    "2024-03-29": "Good Friday", "2024-05-27": "Memorial", "2024-06-19": "Juneteenth",
    "2024-07-04": "Independence", "2024-09-02": "Labor", "2024-11-28": "Thanksgiving",
    "2024-12-25": "Christmas",
    "2025-01-01": "New Year", "2025-01-20": "MLK", "2025-02-17": "Presidents",
    "2025-04-18": "Good Friday", "2025-05-26": "Memorial", "2025-06-19": "Juneteenth",
    "2025-07-04": "Independence", "2025-09-01": "Labor", "2025-11-27": "Thanksgiving",
    "2025-12-25": "Christmas",
    "2026-01-01": "New Year", "2026-01-19": "MLK", "2026-02-16": "Presidents",
    "2026-04-03": "Good Friday", "2026-05-25": "Memorial", "2026-06-19": "Juneteenth",
    "2026-07-03": "Independence (observed)", "2026-09-07": "Labor",
    "2026-11-26": "Thanksgiving", "2026-12-25": "Christmas",
}

US_HALF_DAYS: dict[str, str] = {
    "2024-07-03": "early close", "2024-11-29": "early close", "2024-12-24": "early close",
    "2025-07-03": "early close", "2025-11-28": "early close", "2025-12-24": "early close",
    "2026-11-27": "early close", "2026-12-24": "early close",
}


@dataclass
class Session:
    start: str
    end: str

    @property
    def overnight(self) -> bool:
        return self.start > self.end


@dataclass
class Instrument:
    symbol: str
    name: str
    asset_class: str
    exchange: str = ""
    currency: str = "USD"
    tick_size: float = 0.01
    tick_value: float = 0.01
    multiplier: float = 1.0
    timezone: str = "America/New_York"
    sessions: dict[str, Session] = field(default_factory=dict)
    fees_per_side: float = 0.0
    fee_bps: float = 0.0
    commission_per_share: float = 0.0
    spread_ticks_typical: float = 0.0
    margin_day: float | None = None
    margin_overnight: float | None = None
    expiry: str | None = None
    rollover_days_before: int = 0
    typical_daily_volume: float = 0.0
    typical_daily_range_ticks: float = 0.0
    correlation_group: str = "none"
    prop_eligible: bool = False
    root: str | None = None
    holiday_calendar: str = "none"
    extra: dict[str, Any] = field(default_factory=dict)

    # --------------------------------------------------------------- money
    def ticks_to_currency(self, ticks: float, size: float = 1.0) -> float:
        """The ONLY conversion from dimensionless distance to money."""
        return ticks * self.tick_value * size

    def price_to_ticks(self, price_distance: float) -> float:
        return price_distance / self.tick_size

    def ticks_to_price(self, ticks: float) -> float:
        return ticks * self.tick_size

    def round_to_tick(self, price: float) -> float:
        return round(price / self.tick_size) * self.tick_size

    def cost_per_side(self, price: float, size: float) -> float:
        """Commission + exchange fees for one side, in the instrument's currency."""
        cost = (self.fees_per_side + self.commission_per_share * 0.0) * size
        if self.commission_per_share:
            cost += self.commission_per_share * size
        if self.fee_bps:
            cost += price * size * self.multiplier * self.fee_bps / 10000.0
        return cost

    def notional(self, price: float, size: float = 1.0) -> float:
        return price * self.multiplier * size

    # ------------------------------------------------------------ calendar
    def session_window(self, which: str | None = None) -> Session:
        key = which or "rth"
        return self.sessions.get(key) or self.sessions.get("rth") or Session("00:00", "23:59")

    def is_holiday(self, day: date | str) -> bool:
        if self.holiday_calendar == "none":
            return False
        d = day if isinstance(day, str) else day.isoformat()
        return d in US_HOLIDAYS_2024_2026

    def is_half_day(self, day: date | str) -> bool:
        d = day if isinstance(day, str) else day.isoformat()
        return d in US_HALF_DAYS

    def is_trading_day(self, day: date | str) -> bool:
        d = date.fromisoformat(day) if isinstance(day, str) else day
        if self.asset_class == "crypto":
            return True
        if d.weekday() >= 5:
            return False
        return not self.is_holiday(d)

    def next_rollover(self, from_day: date) -> date | None:
        """Continuous futures contract stitching needs to know when to roll."""
        if self.asset_class != "future" or not self.expiry:
            return None
        months = [3, 6, 9, 12] if self.expiry == "quarterly" else list(range(1, 13))
        y, m = from_day.year, from_day.month
        for _ in range(24):
            if m in months:
                expiry = _third_friday(y, m)
                roll = expiry - timedelta(days=self.rollover_days_before)
                if roll > from_day:
                    return roll
            m += 1
            if m > 12:
                m, y = 1, y + 1
        return None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "asset_class": self.asset_class,
            "tick_size": self.tick_size,
            "tick_value": self.tick_value,
            "multiplier": self.multiplier,
            "currency": self.currency,
            "timezone": self.timezone,
            "correlation_group": self.correlation_group,
            "prop_eligible": self.prop_eligible,
        }


class InstrumentRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or REGISTRY_FILE
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.defaults = raw.get("defaults", {})
        self.correlation_matrix: dict[str, dict[str, float]] = raw.get("correlation_matrix", {})
        self.hedge_threshold: float = float(raw.get("hedge_correlation_threshold", 0.6))
        self._instruments: dict[str, Instrument] = {}
        for symbol, d in (raw.get("instruments") or {}).items():
            self._instruments[symbol.upper()] = self._build(symbol.upper(), d)

    def _build(self, symbol: str, d: dict) -> Instrument:
        asset_class = d.get("asset_class", "equity")
        defaults = self.defaults.get(asset_class, {})
        merged = {**defaults, **d}
        sessions = {k: Session(**v) for k, v in (merged.get("sessions") or {}).items()}
        known = {f for f in Instrument.__dataclass_fields__ if f not in ("symbol", "sessions", "extra")}
        kwargs = {k: v for k, v in merged.items() if k in known}
        extra = {k: v for k, v in merged.items() if k not in known and k != "sessions"}
        kwargs.setdefault("name", symbol)
        return Instrument(symbol=symbol, sessions=sessions, extra=extra, **kwargs)

    # ------------------------------------------------------------- lookup
    def get(self, symbol: str) -> Instrument:
        key = (symbol or "").upper()
        if key in self._instruments:
            return self._instruments[key]
        # continuous/expiry forms: MNQZ5, MNQ1!, NQ-continuous
        for root, inst in self._instruments.items():
            if key.startswith(root) and inst.asset_class == "future":
                return inst
        raise KeyError(
            f"Unknown instrument '{symbol}'. Add it to {self.path.name} -- never special-case it in code."
        )

    def has(self, symbol: str) -> bool:
        try:
            self.get(symbol)
            return True
        except KeyError:
            return False

    def all(self) -> list[Instrument]:
        return list(self._instruments.values())

    def symbols(self) -> list[str]:
        return sorted(self._instruments)

    def by_asset_class(self, asset_class: str) -> list[Instrument]:
        return [i for i in self._instruments.values() if i.asset_class == asset_class]

    # -------------------------------------------------------- correlation
    def correlation(self, a: str, b: str) -> float:
        ga = self.get(a).correlation_group
        gb = self.get(b).correlation_group
        if ga == gb:
            return 1.0
        return float(self.correlation_matrix.get(ga, {}).get(gb, 0.0))

    def is_hedge(self, a: str, b: str) -> bool:
        """Correlation-aware, not symbol-matched (hard rule 3)."""
        return abs(self.correlation(a, b)) >= self.hedge_threshold


@lru_cache(maxsize=1)
def registry() -> InstrumentRegistry:
    return InstrumentRegistry()


def get_instrument(symbol: str) -> Instrument:
    return registry().get(symbol)


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def synthetic_instrument(symbol: str, asset_class: str = "equity", **kw) -> Instrument:
    """For ingested data whose symbol is not in the registry yet. Registered at
    runtime so no downstream module has to care where it came from."""
    inst = Instrument(symbol=symbol.upper(), name=kw.pop("name", symbol), asset_class=asset_class, **kw)
    registry()._instruments[inst.symbol] = inst
    return inst
