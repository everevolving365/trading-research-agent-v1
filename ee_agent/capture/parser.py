"""Turning what the client said into spec fragments.

The client describes their strategy in plain language, at any length, in any
order (ability 8), and rambling, unstructured input is expected and handled
without loss (ability 6).

This module is deterministic and needs no model: it extracts what it can
recognise and, crucially, *reports what it could not*. The interrogation engine
then asks about exactly those gaps. A model, when the client has supplied a key,
is an additional extractor on top -- never a replacement, because a model that
quietly invents a missing rule breaks hard rule 1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ee_agent.instruments.registry import registry
from ee_agent.spec.model import (
    Condition,
    ContextBlock,
    Costs,
    Distance,
    Execution,
    Filters,
    Risk,
    SignalRule,
    Signals,
    Sizing,
    StrategySpec,
    TimeWindow,
    Universe,
)

TIME_RE = re.compile(
    r"\b(?P<h1>\d{1,2})[:.]?(?P<m1>\d{2})?\s*(?P<ap1>am|pm)?\s*(?:to|-|until|through|till)\s*"
    r"(?P<h2>\d{1,2})[:.]?(?P<m2>\d{2})?\s*(?P<ap2>am|pm)?",
    re.I,
)
DISTANCE_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ticks?|points?|pts?|percent|%|atr|r\b|bps|basis points)",
    re.I,
)
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
CONTRACTS_RE = re.compile(
    r"\b(?P<n>\d+|" + "|".join(NUMBER_WORDS) + r")\s*"
    r"(?:contract|contracts|lot|lots|share|shares|micro|micros)\b",
    re.I,
)
NEGATION_RE = re.compile(r"\b(?:don'?t|do not|never|no|without|not)\b", re.I)
MAX_TRADES_RE = re.compile(
    r"(?:max(?:imum)?|no more than|at most|only)\s+(?P<n>\d+)\s+(?:trade|trades|setup|setups)\b", re.I
)
COMMISSION_RE = re.compile(r"(?:commission|fees?)\s*(?:of|is|at)?\s*\$?(?P<v>\d+(?:\.\d+)?)", re.I)
SLIPPAGE_RE = re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*ticks?\s*(?:of\s*)?slippage", re.I)

UNIT_MAP = {
    "tick": "ticks", "ticks": "ticks",
    "point": "points", "points": "points", "pt": "points", "pts": "points",
    "percent": "percent", "%": "percent",
    "atr": "atr", "r": "atr",
    "bps": "bps", "basis points": "bps",
}

TZ_HINTS = {
    "central": "America/Chicago", "chicago": "America/Chicago", "ct": "America/Chicago",
    "cst": "America/Chicago", "cdt": "America/Chicago",
    "eastern": "America/New_York", "new york": "America/New_York", "et": "America/New_York",
    "est": "America/New_York", "edt": "America/New_York",
    "utc": "UTC", "gmt": "UTC", "london": "Europe/London", "pacific": "America/Los_Angeles",
}

ALIASES = {
    "nasdaq": "MNQ", "micro nasdaq": "MNQ", "mini nasdaq": "NQ", "nq": "NQ", "mnq": "MNQ",
    "s&p": "ES", "sp500": "ES", "es": "ES", "mes": "MES", "micro s&p": "MES",
    "bitcoin": "BTCUSDT", "btc": "BTCUSDT", "ether": "ETHUSDT", "eth": "ETHUSDT",
    "spy": "SPY", "qqq": "QQQ", "euro": "EURUSD", "eurusd": "EURUSD", "crude": "CL", "oil": "CL",
}


@dataclass
class ParsedStrategy:
    spec: StrategySpec
    found: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    quotes: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        lines = ["[capture] what I heard:"]
        for key, value in self.found.items():
            quote = self.quotes.get(key, "")
            lines.append(f"    {key:<22} {value}" + (f'   <- "{quote}"' if quote else ""))
        if self.missing:
            lines.append("[capture] what I still need to ask about:")
            lines += [f"    {m}" for m in self.missing]
        return "\n".join(lines)


def _minutes_to_hhmm(hour: int, minute: int, ampm: str | None) -> str:
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    return f"{hour:02d}:{minute:02d}"


def find_time_windows(text: str) -> list[tuple[str, str, str]]:
    """Return (start, end, quote) for each 'X to Y' the client said."""
    out = []
    for m in TIME_RE.finditer(text):
        h1 = int(m.group("h1"))
        h2 = int(m.group("h2"))
        if h1 > 24 or h2 > 24:
            continue
        m1 = int(m.group("m1") or 0)
        m2 = int(m.group("m2") or 0)
        ap1, ap2 = m.group("ap1"), m.group("ap2")
        # "9:30 to 3" in a trading context means 15:00, not 03:00
        if ap2 is None and ap1 is None and h2 < h1 and h2 <= 6:
            ap2 = "pm"
        out.append((_minutes_to_hhmm(h1, m1, ap1), _minutes_to_hhmm(h2, m2, ap2), m.group(0).strip()))
    return out


def find_instruments(text: str) -> list[str]:
    lowered = text.lower()
    found: list[str] = []
    reg = registry()
    for symbol in reg.symbols():
        if re.search(rf"\b{symbol.lower()}\b", lowered):
            found.append(symbol)
    for alias, symbol in ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered) and symbol not in found:
            found.append(symbol)
    return found


def find_timezone(text: str) -> str | None:
    lowered = text.lower()
    for hint, tz in TZ_HINTS.items():
        if re.search(rf"\b{re.escape(hint)}\b", lowered):
            return tz
    return None


def find_distance(text: str, *keywords: str) -> tuple[Distance | None, str]:
    """Find a distance stated near one of the keywords ('stop', 'target').

    Looks FORWARD from the keyword first. People say "my stop is 25 points,
    target is 50 points" -- searching a symmetric window around 'target' finds
    the 25 that belongs to the stop, and silently hands the client a strategy
    they never described.
    """
    lowered = text.lower()
    for keyword in keywords:
        for match in re.finditer(rf"\b{keyword}\w*\b", lowered):
            ahead = text[match.end() : match.end() + 60]
            dm = DISTANCE_RE.search(ahead)
            if dm:
                return _to_distance(dm), text[match.start() : match.end() + 60].strip()
    # Only when nothing follows the keyword do we look behind it.
    for keyword in keywords:
        for match in re.finditer(rf"\b{keyword}\w*\b", lowered):
            behind = text[max(0, match.start() - 40) : match.start()]
            matches = list(DISTANCE_RE.finditer(behind))
            if matches:
                return _to_distance(matches[-1]), (behind + match.group(0)).strip()
    return None, ""


def _to_distance(match: "re.Match") -> Distance:
    raw = match.group("unit").lower()
    unit = UNIT_MAP.get(raw) or UNIT_MAP.get(raw.rstrip("s")) or "ticks"
    return Distance(type=unit, value=float(match.group("value")))


def find_timeframe(text: str) -> str | None:
    m = re.search(r"\b(\d+)\s*(?:-)?\s*(minute|min|m|hour|h)\b(?:\s*(?:chart|bars?|candles?))?", text, re.I)
    if not m:
        return None
    n, unit = m.group(1), m.group(2).lower()
    return f"{n}m" if unit.startswith("m") else f"{n}h"


def parse(text: str, strategy_id: str = "captured-v1", name: str = "") -> ParsedStrategy:
    """Extract everything recognisable. Never guess: what is absent is reported."""
    found: dict[str, str] = {}
    quotes: dict[str, str] = {}
    missing: list[str] = []
    lowered = text.lower()

    instruments = find_instruments(text)
    if instruments:
        found["instruments"] = ", ".join(instruments)
    else:
        missing.append("which instruments this trades")

    tz = find_timezone(text)
    if tz:
        found["timezone"] = tz

    windows = find_time_windows(text)
    context: list[ContextBlock] = []
    filters = Filters()
    if len(windows) >= 1:
        start, end, quote = windows[0]
        if any(w in lowered for w in ("opening range", "open range", "or ", "first hour", "range from")):
            context.append(ContextBlock(id="opening_range", type="session_range",
                                        params={"window": {"start": start, "end": end}}))
            found["opening_range"] = f"{start}-{end}"
            quotes["opening_range"] = quote
    if len(windows) >= 2:
        start, end, quote = windows[1]
        filters.time_windows.append(TimeWindow(start=start, end=end, tz=tz))
        found["signal_window"] = f"{start}-{end}"
        quotes["signal_window"] = quote
    elif len(windows) == 1 and not context:
        start, end, quote = windows[0]
        filters.time_windows.append(TimeWindow(start=start, end=end, tz=tz))
        found["signal_window"] = f"{start}-{end}"
        quotes["signal_window"] = quote
    if not filters.time_windows:
        missing.append("which hours this trades")

    mt = MAX_TRADES_RE.search(text)
    if mt:
        filters.max_trades_per_day = int(mt.group("n"))
        found["max_trades_per_day"] = mt.group("n")
        quotes["max_trades_per_day"] = mt.group(0)
    else:
        missing.append("how many times a day you will take it")

    timeframe = find_timeframe(text) or "5m"
    found["timeframe"] = timeframe

    # ---- signals ------------------------------------------------------
    entry: list[SignalRule] = []
    sweep_words = ("sweep", "break", "breaks", "takes out", "runs the", "pokes")
    return_words = ("return", "returns", "comes back", "back inside", "reclaim", "reclaims", "back in")
    if context and any(w in lowered for w in sweep_words) and any(w in lowered for w in return_words):
        found["pattern"] = "break of the range then a return inside"
        for side, rule_side in (("high", "short"), ("low", "long")):
            entry.append(
                SignalRule(
                    id=rule_side,
                    side=rule_side,
                    timeframe=timeframe,
                    all_of=[
                        Condition("range_break", {"reference": "opening_range", "side": side, "confirm": "wick"}),
                        Condition("return_inside", {"reference": "opening_range", "confirm": "close"}),
                    ],
                )
            )
    else:
        ma = re.search(r"(\d+)\s*(?:period\s*)?(ema|sma)\D{0,30}?(\d+)\s*(?:period\s*)?(?:ema|sma)", text, re.I)
        if ma:
            fast, kind, slow = int(ma.group(1)), ma.group(2).lower(), int(ma.group(3))
            found["pattern"] = f"{kind.upper()} {fast}/{slow} cross"
            entry.append(
                SignalRule(
                    id="long", side="long", timeframe=timeframe,
                    all_of=[Condition("ma_cross", {"fast": fast, "slow": slow, "ma": kind, "direction": "up"})],
                )
            )
            entry.append(
                SignalRule(
                    id="short", side="short", timeframe=timeframe,
                    all_of=[Condition("ma_cross", {"fast": fast, "slow": slow, "ma": kind, "direction": "down"})],
                )
            )
        else:
            missing.append("exactly what fires the entry")

    # ---- risk ---------------------------------------------------------
    risk = Risk()
    stop, stop_quote = find_distance(text, "stop", "risk", "invalidat")
    if stop:
        risk.stop = stop
        found["stop"] = f"{stop.value} {stop.type}"
        quotes["stop"] = stop_quote
    else:
        missing.append("where the stop goes")
    target, target_quote = find_distance(text, "target", "profit", "take profit", "tp")
    if target:
        risk.target = target
        found["target"] = f"{target.value} {target.type}"
        quotes["target"] = target_quote
    else:
        missing.append("where you take profit")

    cm = CONTRACTS_RE.search(text)
    if cm:
        raw = cm.group("n").lower()
        value = float(NUMBER_WORDS[raw]) if raw in NUMBER_WORDS else float(raw)
        risk.size = Sizing(method="fixed_contracts", value=value)
        found["size"] = f"{value:g} contracts"
        quotes["size"] = cm.group(0)
    scale_match = re.search(r"\bscal(?:e|ing)\s+in\b", lowered)
    if scale_match:
        # "I don't scale in" and "I scale in" are opposite rules. A missed
        # negation silently changes the client's position sizing.
        preceding = lowered[max(0, scale_match.start() - 30) : scale_match.start()]
        risk.scale_in = not bool(NEGATION_RE.search(preceding))
        found["scale_in"] = str(risk.scale_in)
        quotes["scale_in"] = (preceding + scale_match.group(0)).strip()

    # ---- execution ----------------------------------------------------
    execution = Execution()
    if any(w in lowered for w in ("close of the bar", "candle close", "bar close", "on close", "wait for the close")):
        execution.bar_close_confirmation = True
        found["confirmation"] = "waits for the candle close"
    if any(w in lowered for w in ("flat by the close", "flatten", "close it at the end", "flat at the end",
                                  "out by the close", "never hold overnight", "no overnight")):
        execution.session_close_behavior = "flatten"
        found["session_close"] = "flatten at the end of the window"
    if "ignore" in lowered and "signal" in lowered:
        execution.on_overlapping_signal = "ignore_new"

    # ---- costs --------------------------------------------------------
    costs = Costs(commission_per_side=0.0, slippage_ticks=0.0, spread_ticks=0.0)
    cm2 = COMMISSION_RE.search(text)
    if cm2:
        costs.commission_per_side = float(cm2.group("v"))
        found["commission"] = f"{costs.commission_per_side} per side"
    sm = SLIPPAGE_RE.search(text)
    if sm:
        costs.slippage_ticks = float(sm.group("v"))
        found["slippage"] = f"{costs.slippage_ticks} ticks"
    if costs.commission_per_side == 0 and costs.slippage_ticks == 0:
        missing.append("your commission and slippage (I will not run a frictionless backtest)")

    spec = StrategySpec(
        id=strategy_id,
        name=name or strategy_id.replace("-", " ").title(),
        universe=Universe(
            instruments=instruments,
            session="rth" if "regular" in lowered or "rth" in lowered else "rth",
            timezone=tz or "America/Chicago",
        ),
        context=context,
        signals=Signals(
            entry=entry,
            invalidation=[Condition("session_end", {"reference": "signal_window"})]
            if execution.session_close_behavior == "flatten"
            else [],
        ),
        filters=filters,
        risk=risk,
        execution=execution,
        costs=costs,
    )
    return ParsedStrategy(spec=spec, found=found, missing=missing, quotes=quotes)
