"""Searching the web for data sources the agent does not already know about.

Ability 17. The agent hits an asset it has no source for, goes and looks, and
comes back with candidates: what each covers, what it costs, whether it needs a
key, and how to get one.

What this does NOT do, deliberately
-----------------------------------
It does not scrape arbitrary websites for price data. Section 9, honest limit 2:
that will not hold -- sites break, terms of service prohibit it, and a strategy
resting on a scraper is a strategy that stops working without warning. So
discovery finds *sources* (APIs, vendors, exports), and the client then either
supplies a key or downloads a file the ingestion layer reads. The capability is
delivered; the fragile mechanism is not.

Two layers, and the first needs no network at all:

* a curated catalogue of real data vendors, searched locally;
* a live web search, when the client has a search provider configured, for
  anything the catalogue does not cover.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from ee_agent.cost.notifier import ledger
from ee_agent.paths import ee_home
from ee_agent.secrets.vault import get_secret

CATALOGUE_FILE = Path(__file__).with_name("source_catalogue.json")


@dataclass
class SourceCandidate:
    name: str
    url: str
    asset_classes: list[str] = field(default_factory=list)
    resolutions: list[str] = field(default_factory=list)
    history: str = ""
    cost: str = ""
    needs_key: bool = True
    key_url: str = ""
    has_order_flow: bool = False
    access: str = "api"  # api | download | export | scrape
    notes: str = ""
    score: float = 0.0
    found_by: str = "catalogue"

    def to_dict(self) -> dict:
        return asdict(self)

    def line(self) -> str:
        key = "needs a key" if self.needs_key else "no key needed"
        flow = ", real order flow" if self.has_order_flow else ""
        return (
            f"  {self.name:<22} {self.cost:<22} {key:<14} {self.access:<9}{flow}\n"
            f"      {self.url}\n"
            f"      covers {', '.join(self.asset_classes)} at {', '.join(self.resolutions[:5])}"
            + (f"; {self.history}" if self.history else "")
        )


@dataclass
class DiscoveryResult:
    query: str
    asset_class: str | None
    candidates: list[SourceCandidate] = field(default_factory=list)
    searched_web: bool = False
    search_note: str = ""
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "asset_class": self.asset_class,
            "searched_web": self.searched_web,
            "search_note": self.search_note,
            "cost_usd": round(self.cost_usd, 6),
            "candidates": [c.to_dict() for c in self.candidates],
            "already_wired_in": [c.name for c in self.candidates if c.notes.startswith("Already")],
        }

    def summary(self) -> str:
        if not self.candidates:
            return (
                f"[discovery] nothing found for {self.query!r}. {self.search_note}\n"
                "            You can still use it: download a file from anywhere and run "
                "`ee-agent data ingest <path>` -- any format, schema inferred."
            )
        lines = [f"[discovery] {len(self.candidates)} source(s) for {self.query!r}:"]
        lines += [c.line() for c in self.candidates]
        if self.search_note:
            lines.append(f"            {self.search_note}")
        lines.append(
            "            Whatever you pick: if it needs paying for, I'll bring the page to you, "
            "tell you what it unlocks for your strategy and what the free fallback costs. "
            "You pay, I carry on."
        )
        return "\n".join(lines)


# =============================================================== catalogue
def _catalogue() -> list[SourceCandidate]:
    if not CATALOGUE_FILE.exists():
        return []
    raw = json.loads(CATALOGUE_FILE.read_text(encoding="utf-8"))
    return [SourceCandidate(**entry, found_by="catalogue") for entry in raw.get("sources", [])]


ASSET_WORDS = {
    "future": ["future", "futures", "cme", "nymex", "cbot", "comex", "mnq", "nq", "es", "cl", "gc", "contract"],
    "crypto": ["crypto", "bitcoin", "btc", "eth", "ether", "binance", "coinbase", "token", "altcoin", "perp"],
    "equity": ["stock", "stocks", "equity", "equities", "share", "shares", "etf", "nasdaq", "nyse"],
    "forex": ["forex", "fx", "currency", "currencies", "eurusd", "gbpusd", "pair", "pairs"],
    "index": ["index", "indices", "spx", "ndx", "vix", "dax", "ftse"],
    "option": ["option", "options", "calls", "puts", "greeks", "implied volatility"],
    "commodity": ["commodity", "commodities", "copper", "gold", "silver", "oil", "wheat", "corn"],
}

RESOLUTION_WORDS = {
    "tick": ["tick", "ticks", "trade by trade", "every trade", "l2", "level 2", "order book", "depth"],
    "1s": ["second", "seconds", "1s"],
    "1m": ["minute", "1-minute", "1 minute", "1m", "minutely", "intraday"],
    "5m": ["5m", "5-minute", "5 minute"],
    "1h": ["hour", "hourly", "1h"],
    "1d": ["daily", "day", "1d", "eod", "end of day"],
}


def infer_asset_class(query: str) -> str | None:
    lowered = query.lower()
    best, best_hits = None, 0
    for asset_class, words in ASSET_WORDS.items():
        hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", lowered))
        if hits > best_hits:
            best, best_hits = asset_class, hits
    return best


def infer_resolutions(query: str) -> list[str]:
    lowered = query.lower()
    out = []
    for resolution, words in RESOLUTION_WORDS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in words):
            out.append(resolution)
    return out


def _score(candidate: SourceCandidate, query: str, asset_class: str | None, resolutions: list[str]) -> float:
    score = 0.0
    lowered = query.lower()
    if asset_class and asset_class in candidate.asset_classes:
        score += 5.0
    elif asset_class:
        return 0.0  # it does not serve this asset class at all
    for resolution in resolutions:
        if resolution in candidate.resolutions:
            score += 2.0
        else:
            score -= 1.0  # asked for tick data, this one has none
    if not candidate.needs_key:
        score += 1.5  # free and keyless beats paid, all else equal
    if candidate.has_order_flow and any(
        w in lowered for w in ("order flow", "delta", "footprint", "absorption", "tick")
    ):
        score += 3.0
    if candidate.name.lower() in lowered:
        score += 4.0
    if candidate.access == "scrape":
        score -= 3.0  # Section 9: scraping does not hold
    return score


def search_catalogue(query: str, asset_class: str | None = None, limit: int = 6) -> list[SourceCandidate]:
    """The offline half: no network, no key, instant."""
    asset_class = asset_class or infer_asset_class(query)
    resolutions = infer_resolutions(query)
    scored = []
    for candidate in _catalogue():
        candidate.score = _score(candidate, query, asset_class, resolutions)
        if candidate.score > 0:
            scored.append(candidate)
    scored.sort(key=lambda c: -c.score)
    return scored[:limit]


# ============================================================== web search
def _web_search(query: str, limit: int = 6) -> tuple[list[dict], str]:
    """Live search, if the client has a provider configured.

    Supported without shipping anyone's key: Brave, Tavily, SerpAPI. Absent all
    three, the catalogue still answers and this says why it did not search.
    """
    try:
        import requests
    except ImportError:
        return [], "the `requests` package is not installed, so I could not search the web"

    brave = get_secret("BRAVE_SEARCH_API_KEY")
    if brave:
        try:
            response = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": limit},
                headers={"X-Subscription-Token": brave, "Accept": "application/json"},
                timeout=20,
            )
            response.raise_for_status()
            results = response.json().get("web", {}).get("results", [])
            return (
                [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
                 for r in results],
                "searched the web with Brave",
            )
        except Exception as exc:
            return [], f"Brave search failed: {exc}"

    tavily = get_secret("TAVILY_API_KEY")
    if tavily:
        try:
            response = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": tavily, "query": query, "max_results": limit},
                timeout=20,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            return (
                [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
                 for r in results],
                "searched the web with Tavily",
            )
        except Exception as exc:
            return [], f"Tavily search failed: {exc}"

    serp = get_secret("SERPAPI_API_KEY")
    if serp:
        try:
            response = requests.get(
                "https://serpapi.com/search",
                params={"q": query, "api_key": serp, "num": limit},
                timeout=20,
            )
            response.raise_for_status()
            results = response.json().get("organic_results", [])
            return (
                [{"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")}
                 for r in results],
                "searched the web with SerpAPI",
            )
        except Exception as exc:
            return [], f"SerpAPI search failed: {exc}"

    return [], (
        "no web search key is configured, so I answered from the built-in catalogue only. "
        "Add one with `ee-agent secrets set BRAVE_SEARCH_API_KEY` (or TAVILY_API_KEY / "
        "SERPAPI_API_KEY) and I will go and look."
    )


VENDOR_HINTS = {
    "databento": ("future", True), "polygon": ("equity", False), "tiingo": ("equity", False),
    "alpaca": ("equity", False), "iex": ("equity", False), "finnhub": ("equity", False),
    "twelvedata": ("equity", False), "eodhd": ("equity", False), "intrinio": ("equity", False),
    "kibot": ("future", False), "firstrate": ("future", False), "dukascopy": ("forex", False),
    "histdata": ("forex", False), "truefx": ("forex", False), "oanda": ("forex", False),
    "binance": ("crypto", True), "coinbase": ("crypto", True), "kraken": ("crypto", True),
    "bybit": ("crypto", True), "kaiko": ("crypto", True), "tardis": ("crypto", True),
    "quandl": ("commodity", False), "nasdaq data link": ("commodity", False),
    "barchart": ("future", False), "cqg": ("future", True), "rithmic": ("future", True),
    "tradestation": ("future", False), "interactive brokers": ("equity", False),
}


def _candidates_from_web(results: list[dict], asset_class: str | None) -> list[SourceCandidate]:
    """Turn search hits into candidates, conservatively.

    Only hits that look like an actual data vendor become candidates. A blog
    post about where to get data is not a data source, and offering it as one
    would waste the client's time.
    """
    known = {c.name.lower() for c in _catalogue()}
    out: list[SourceCandidate] = []
    seen: set[str] = set()
    for hit in results:
        haystack = f"{hit.get('title', '')} {hit.get('url', '')} {hit.get('snippet', '')}".lower()
        for vendor, (vendor_class, has_flow) in VENDOR_HINTS.items():
            if vendor not in haystack or vendor in seen:
                continue
            seen.add(vendor)
            already = vendor in known
            out.append(
                SourceCandidate(
                    name=vendor.title(),
                    url=hit.get("url", ""),
                    asset_classes=[asset_class or vendor_class],
                    resolutions=["1m", "1d"],
                    cost="see their pricing page",
                    needs_key=True,
                    has_order_flow=has_flow,
                    access="api",
                    notes=(
                        "Already wired into the source registry."
                        if already
                        else f"Found on the web: {hit.get('title', '')[:90]}. Not yet wired in -- "
                        "download a file from them and `ee-agent data ingest` reads it, or ask me "
                        "to add a fetcher."
                    ),
                    score=1.0,
                    found_by="web",
                )
            )
    return out


# ================================================================ entry point
def discover_sources(
    query: str, asset_class: str | None = None, limit: int = 6, use_web: bool = True
) -> DiscoveryResult:
    """Find where to get data for something the agent has no source for."""
    asset_class = asset_class or infer_asset_class(query)
    result = DiscoveryResult(query=query, asset_class=asset_class)
    result.candidates = search_catalogue(query, asset_class=asset_class, limit=limit)

    if use_web:
        spend_before = ledger().total_usd
        ledger().spend("data_fetch", **{"model.input_1k": 0.5})
        hits, note = _web_search(f"{query} historical market data API", limit=limit)
        result.searched_web = bool(hits)
        result.search_note = note
        result.cost_usd = ledger().total_usd - spend_before
        existing = {c.name.lower() for c in result.candidates}
        for candidate in _candidates_from_web(hits, asset_class):
            if candidate.name.lower() not in existing:
                result.candidates.append(candidate)

    _log(result)
    return result


def _log(result: DiscoveryResult) -> None:
    """Discovery compounds too: what was found once is worth keeping."""
    try:
        path = ee_home() / "source-discovery.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **result.to_dict()}, default=str)
                + "\n"
            )
    except OSError:
        pass
