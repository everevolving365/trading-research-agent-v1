"""The data source registry and its capability matrix (ability 19).

Which sources serve which asset classes, at which resolutions, how far back, at
what cost, with what rate limits and what reliability. The resolver picks the
best available source for a request and falls down the ladder on failure.

Adding a source is adding an entry here plus a fetcher class. No caller ever
names a source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ee_agent.secrets.vault import has_secret


@dataclass
class SourceCapability:
    name: str
    asset_classes: list[str]
    resolutions: list[str]
    history_days: int
    cost_per_1k_bars_usd: float
    rate_limit_per_min: int
    reliability: float  # 0..1, observed
    requires_secret: str | None = None
    has_flow: bool = False
    notes: str = ""
    paywall_url: str | None = None

    @property
    def keyless(self) -> bool:
        return self.requires_secret is None

    @property
    def available(self) -> bool:
        return self.keyless or has_secret(self.requires_secret or "")

    def serves(self, asset_class: str, resolution: str, days_back: int) -> bool:
        return (
            asset_class in self.asset_classes
            and resolution in self.resolutions
            and days_back <= self.history_days
        )

    def row(self) -> str:
        key = self.requires_secret or "-"
        return (
            f"{self.name:<12} {','.join(self.asset_classes):<28} {','.join(self.resolutions):<22} "
            f"{self.history_days:>6}d  ${self.cost_per_1k_bars_usd:<7.3f} {self.rate_limit_per_min:>5}/m  "
            f"{self.reliability:.2f}  {key:<20} {'FLOW' if self.has_flow else '    '}  "
            f"{'READY' if self.available else 'needs key'}"
        )


CAPABILITIES: dict[str, SourceCapability] = {
    "fixture": SourceCapability(
        name="fixture",
        asset_classes=["future", "crypto", "equity", "forex", "index"],
        resolutions=["1s", "1m", "5m", "15m", "1h", "1d"],
        history_days=36500,
        cost_per_1k_bars_usd=0.0,
        rate_limit_per_min=10**6,
        reliability=1.0,
        has_flow=True,
        notes="Recorded responses committed to tests/fixtures. The whole build runs on these.",
    ),
    "csv_ingest": SourceCapability(
        name="csv_ingest",
        asset_classes=["future", "crypto", "equity", "forex", "index", "option"],
        resolutions=["tick", "1s", "1m", "5m", "15m", "1h", "1d"],
        history_days=36500,
        cost_per_1k_bars_usd=0.0,
        rate_limit_per_min=10**6,
        reliability=1.0,
        notes="Any file the client already has. Universal ingestion (ability 18).",
    ),
    "binance": SourceCapability(
        name="binance",
        asset_classes=["crypto"],
        resolutions=["1m", "5m", "15m", "1h", "1d"],
        history_days=3650,
        cost_per_1k_bars_usd=0.0,
        rate_limit_per_min=1200,
        reliability=0.97,
        requires_secret=None,
        notes="Keyless public klines. This is what makes the free-tier demo possible.",
    ),
    "yfinance": SourceCapability(
        name="yfinance",
        asset_classes=["equity", "index", "crypto"],
        resolutions=["1m", "5m", "15m", "1h", "1d"],
        history_days=730,
        cost_per_1k_bars_usd=0.0,
        rate_limit_per_min=120,
        reliability=0.80,
        notes="Free fallback. 1m data limited to ~7 recent days; unofficial endpoint, breaks periodically.",
    ),
    "polygon": SourceCapability(
        name="polygon",
        asset_classes=["equity", "index", "forex", "option"],
        resolutions=["1s", "1m", "5m", "15m", "1h", "1d"],
        history_days=7300,
        cost_per_1k_bars_usd=0.004,
        rate_limit_per_min=300,
        reliability=0.96,
        requires_secret="POLYGON_API_KEY",
        paywall_url="https://polygon.io/pricing",
    ),
    "databento": SourceCapability(
        name="databento",
        asset_classes=["future"],
        resolutions=["tick", "1s", "1m", "5m", "15m", "1h", "1d"],
        history_days=7300,
        cost_per_1k_bars_usd=0.02,
        rate_limit_per_min=60,
        reliability=0.99,
        requires_secret="DATABENTO_API_KEY",
        has_flow=True,
        paywall_url="https://databento.com/pricing",
        notes="The only source here with real order flow. Tick data is 1-3 orders of magnitude "
        "more expensive than bars -- the cost notifier says so before every pull.",
    ),
}


def capability_matrix() -> str:
    header = (
        f"{'SOURCE':<12} {'ASSET CLASSES':<28} {'RESOLUTIONS':<22} {'BACK':>7}  {'$/1k':<8} "
        f"{'RATE':>7}  {'REL':<5} {'KEY':<20} FLOW  STATUS"
    )
    return "\n".join([header, "-" * len(header), *(c.row() for c in CAPABILITIES.values())])


def ladder(asset_class: str, resolution: str, days_back: int, prefer: list[str] | None = None) -> list[str]:
    """Return the ordered fallback ladder for a request.

    Ordering: client preference first, then by availability, then reliability,
    then cost. A source that cannot serve the request never appears.
    """
    candidates = [c for c in CAPABILITIES.values() if c.serves(asset_class, resolution, days_back)]
    prefer = prefer or []

    def key(c: SourceCapability):
        pref_rank = prefer.index(c.name) if c.name in prefer else len(prefer)
        return (pref_rank, 0 if c.available else 1, -c.reliability, c.cost_per_1k_bars_usd)

    return [c.name for c in sorted(candidates, key=key)]


#: fetcher classes register themselves here at import time
FETCHERS: dict[str, Callable] = {}


def register_fetcher(name: str, factory: Callable) -> None:
    FETCHERS[name] = factory


def get_fetcher(name: str):
    if name not in FETCHERS:
        raise KeyError(f"No fetcher registered for source '{name}'")
    return FETCHERS[name]()
