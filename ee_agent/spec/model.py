"""THE STRATEGY SPEC -- the single source of truth (Section 4, Law Two).

Everything compiles from this object: the Python backtest, the Pine indicator,
the Pine strategy and the live config. Nothing is ever translated twice.

Two properties make it portable:

* **Dimensionless.** Every distance is in ticks, ATR multiples, percent or
  standard deviations -- never raw currency. That is what lets one spec run on
  MNQ and on BTCUSDT with no edit (Phase 2 acceptance).
* **Complete.** No ambiguity survives into the spec. Unresolved questions live
  in an explicit ``assumptions`` block with the client's approval recorded.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Literal

import yaml

SPEC_VERSION = 1

# Dimensionless units only. `currency` is deliberately absent from distances.
DistanceUnit = Literal["ticks", "points", "percent", "atr", "stdev", "bps"]
SizeMethod = Literal["fixed_contracts", "risk_percent", "risk_ticks", "fixed_notional"]


def _clean(obj: Any) -> Any:
    """Drop Nones so a round-trip through YAML is stable and diffs stay small."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if v is not None and v != []}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


# ----------------------------------------------------------------- components
@dataclass
class Universe:
    instruments: list[str] = field(default_factory=list)
    session: str = "rth"  # rth | eth | 24h | custom
    timezone: str = "America/Chicago"


@dataclass
class ContextBlock:
    """A named piece of derived state a signal can reference, e.g. an opening range."""

    id: str
    type: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ContextBlock":
        d = dict(d)
        cid, ctype = d.pop("id"), d.pop("type")
        # `window: {start, end}` and friends stay as free-form params
        return cls(id=cid, type=ctype, params=d)

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, **self.params}


@dataclass
class Condition:
    """One invocation of a registered primitive."""

    type: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Condition":
        d = dict(d)
        return cls(type=d.pop("type"), params=d)

    def to_dict(self) -> dict:
        return {"type": self.type, **self.params}


@dataclass
class SignalRule:
    id: str
    side: Literal["long", "short"] | None = None
    timeframe: str = "5m"
    all_of: list[Condition] = field(default_factory=list)
    any_of: list[Condition] = field(default_factory=list)
    none_of: list[Condition] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "SignalRule":
        return cls(
            id=d["id"],
            side=d.get("side"),
            timeframe=d.get("timeframe", "5m"),
            all_of=[Condition.from_dict(c) for c in d.get("all_of", [])],
            any_of=[Condition.from_dict(c) for c in d.get("any_of", [])],
            none_of=[Condition.from_dict(c) for c in d.get("none_of", [])],
        )

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"id": self.id, "timeframe": self.timeframe}
        if self.side:
            out["side"] = self.side
        for key, conds in (("all_of", self.all_of), ("any_of", self.any_of), ("none_of", self.none_of)):
            if conds:
                out[key] = [c.to_dict() for c in conds]
        return out

    @property
    def conditions(self) -> list[Condition]:
        return [*self.all_of, *self.any_of, *self.none_of]


@dataclass
class Signals:
    entry: list[SignalRule] = field(default_factory=list)
    exit: list[SignalRule] = field(default_factory=list)
    invalidation: list[Condition] = field(default_factory=list)


@dataclass
class TimeWindow:
    start: str
    end: str
    tz: str | None = None


@dataclass
class Filters:
    time_windows: list[TimeWindow] = field(default_factory=list)
    max_trades_per_day: int | None = None
    days_of_week: list[str] = field(default_factory=list)
    blackout_events: list[str] = field(default_factory=list)  # e.g. FOMC, CPI


@dataclass
class Distance:
    """A dimensionless distance. ``{type: atr, value: 1.5}``."""

    type: DistanceUnit = "ticks"
    value: float = 0.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "Distance | None":
        if d is None:
            return None
        return cls(type=d.get("type", "ticks"), value=float(d.get("value", 0)))

    def to_dict(self) -> dict:
        return {"type": self.type, "value": self.value}


@dataclass
class Sizing:
    method: SizeMethod = "fixed_contracts"
    value: float = 1.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "Sizing":
        d = d or {}
        return cls(method=d.get("method", "fixed_contracts"), value=float(d.get("value", 1)))

    def to_dict(self) -> dict:
        return {"method": self.method, "value": self.value}


@dataclass
class Risk:
    """The client's risk rules, recorded exactly as given and applied unaltered
    (ability 12, hard rule 1). The agent never edits this block."""

    stop: Distance | None = None
    target: Distance | None = None
    trail: Distance | None = None
    breakeven_at: Distance | None = None
    size: Sizing = field(default_factory=Sizing)
    max_concurrent_positions: int = 1
    scale_in: bool = False
    daily_loss_limit: Distance | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Risk":
        d = d or {}
        return cls(
            stop=Distance.from_dict(d.get("stop")),
            target=Distance.from_dict(d.get("target")),
            trail=Distance.from_dict(d.get("trail")),
            breakeven_at=Distance.from_dict(d.get("breakeven_at")),
            size=Sizing.from_dict(d.get("size")),
            max_concurrent_positions=int(d.get("max_concurrent_positions", 1)),
            scale_in=bool(d.get("scale_in", False)),
            daily_loss_limit=Distance.from_dict(d.get("daily_loss_limit")),
        )

    def to_dict(self) -> dict:
        return _clean(
            {
                "stop": self.stop.to_dict() if self.stop else None,
                "target": self.target.to_dict() if self.target else None,
                "trail": self.trail.to_dict() if self.trail else None,
                "breakeven_at": self.breakeven_at.to_dict() if self.breakeven_at else None,
                "size": self.size.to_dict(),
                "max_concurrent_positions": self.max_concurrent_positions,
                "scale_in": self.scale_in,
                "daily_loss_limit": self.daily_loss_limit.to_dict() if self.daily_loss_limit else None,
            }
        )


@dataclass
class Execution:
    entry_order: str = "market_on_close_of_signal_bar"
    session_close_behavior: str = "flatten"  # flatten | hold | flatten_at
    on_overlapping_signal: str = "ignore_new"  # ignore_new | replace | stack
    gap_behavior: str = "fill_at_open"  # fill_at_open | skip
    bar_close_confirmation: bool = True


@dataclass
class Costs:
    """No backtest runs without this. Hard rule 7."""

    commission_per_side: float = 0.0  # currency per contract/share, per side
    slippage_ticks: float = 1.0
    spread_ticks: float = 0.0
    slippage_model: str = "volume_volatility"  # fixed | volume_volatility
    exchange_fees_per_side: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "Costs":
        d = d or {}

        def unitval(key: str, default: float) -> float:
            v = d.get(key, default)
            if isinstance(v, dict):
                return float(v.get("value", default))
            return float(v)

        return cls(
            commission_per_side=unitval("commission_per_side", 0.0),
            slippage_ticks=unitval("slippage", 1.0) if "slippage" in d else unitval("slippage_ticks", 1.0),
            spread_ticks=unitval("spread", 0.0) if "spread" in d else unitval("spread_ticks", 0.0),
            slippage_model=d.get("slippage_model", "volume_volatility"),
            exchange_fees_per_side=unitval("exchange_fees_per_side", 0.0),
        )

    def to_dict(self) -> dict:
        return {
            "commission_per_side": {"unit": "currency", "value": self.commission_per_side},
            "slippage": {"unit": "ticks", "value": self.slippage_ticks},
            "spread": {"unit": "ticks", "value": self.spread_ticks},
            "slippage_model": self.slippage_model,
            "exchange_fees_per_side": {"unit": "currency", "value": self.exchange_fees_per_side},
        }


@dataclass
class Assumption:
    """The ambiguity ledger (ability 13). Every assumption logged, shown and
    measured -- ``sensitivity`` records what changes if it is wrong."""

    id: str
    question: str
    resolution: str
    approved_by: str = "pending"
    alternatives: list[str] = field(default_factory=list)
    sensitivity: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.approved_by not in ("pending", "", None)


@dataclass
class StrategySpec:
    id: str
    name: str
    spec_version: int = SPEC_VERSION
    author: str = "client"
    created: str = field(default_factory=lambda: date.today().isoformat())
    source_transcript: str | None = None
    universe: Universe = field(default_factory=Universe)
    context: list[ContextBlock] = field(default_factory=list)
    signals: Signals = field(default_factory=Signals)
    filters: Filters = field(default_factory=Filters)
    risk: Risk = field(default_factory=Risk)
    execution: Execution = field(default_factory=Execution)
    costs: Costs = field(default_factory=Costs)
    assumptions: list[Assumption] = field(default_factory=list)
    notes: str = ""

    # ------------------------------------------------------------ round trip
    @classmethod
    def from_dict(cls, d: dict) -> "StrategySpec":
        sig = d.get("signals", {}) or {}
        flt = d.get("filters", {}) or {}
        ex = d.get("execution", {}) or {}
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            spec_version=int(d.get("spec_version", SPEC_VERSION)),
            author=d.get("author", "client"),
            created=str(d.get("created", date.today().isoformat())),
            source_transcript=d.get("source_transcript"),
            universe=Universe(**(d.get("universe") or {})),
            context=[ContextBlock.from_dict(c) for c in d.get("context", [])],
            signals=Signals(
                entry=[SignalRule.from_dict(s) for s in sig.get("entry", [])],
                exit=[SignalRule.from_dict(s) for s in sig.get("exit", [])],
                invalidation=[Condition.from_dict(c) for c in sig.get("invalidation", [])],
            ),
            filters=Filters(
                time_windows=[TimeWindow(**w) for w in flt.get("time_windows", [])],
                max_trades_per_day=flt.get("max_trades_per_day"),
                days_of_week=flt.get("days_of_week", []),
                blackout_events=flt.get("blackout_events", []),
            ),
            risk=Risk.from_dict(d.get("risk", {})),
            execution=Execution(**ex) if ex else Execution(),
            costs=Costs.from_dict(d.get("costs", {})),
            assumptions=[Assumption(**a) for a in d.get("assumptions", [])],
            notes=d.get("notes", ""),
        )

    def to_dict(self) -> dict:
        return _clean(
            {
                "spec_version": self.spec_version,
                "id": self.id,
                "name": self.name,
                "author": self.author,
                "created": self.created,
                "source_transcript": self.source_transcript,
                "universe": asdict(self.universe),
                "context": [c.to_dict() for c in self.context],
                "signals": {
                    "entry": [s.to_dict() for s in self.signals.entry],
                    "exit": [s.to_dict() for s in self.signals.exit],
                    "invalidation": [c.to_dict() for c in self.signals.invalidation],
                },
                "filters": {
                    "time_windows": [asdict(w) for w in self.filters.time_windows],
                    "max_trades_per_day": self.filters.max_trades_per_day,
                    "days_of_week": self.filters.days_of_week,
                    "blackout_events": self.filters.blackout_events,
                },
                "risk": self.risk.to_dict(),
                "execution": asdict(self.execution),
                "costs": self.costs.to_dict(),
                "assumptions": [asdict(a) for a in self.assumptions],
                "notes": self.notes or None,
            }
        )

    # ------------------------------------------------------------------ yaml
    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True, width=100)

    @classmethod
    def from_yaml(cls, text: str) -> "StrategySpec":
        return cls.from_dict(yaml.safe_load(text))

    @classmethod
    def load(cls, path) -> "StrategySpec":
        from pathlib import Path

        return cls.from_yaml(Path(path).read_text(encoding="utf-8"))

    def save(self, path) -> None:
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(self.to_yaml(), encoding="utf-8")

    # ----------------------------------------------------------- identity
    @property
    def hash(self) -> str:
        """Content hash. Two specs with the same hash are the same strategy,
        which is what the parity proof and the research index key on."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def short_hash(self) -> str:
        return self.hash.split(":")[1][:12]

    # ----------------------------------------------------------- inspection
    def all_conditions(self) -> list[Condition]:
        out: list[Condition] = []
        for rule in [*self.signals.entry, *self.signals.exit]:
            out.extend(rule.conditions)
        out.extend(self.signals.invalidation)
        return out

    def primitive_types(self) -> list[str]:
        return sorted({c.type for c in self.all_conditions()} | {c.type for c in self.context})

    def unresolved_assumptions(self) -> list[Assumption]:
        return [a for a in self.assumptions if not a.approved]

    def describe(self) -> str:
        """Plain English, so a client can look at it and recognise their own
        strategy (Section 4, requirement 4)."""
        lines = [f"{self.name}  [{self.id} @ {self.short_hash}]"]
        lines.append(
            f"  Trades {', '.join(self.universe.instruments)} on the {self.universe.session.upper()} "
            f"session, {self.universe.timezone}."
        )
        for c in self.context:
            lines.append(f"  Context '{c.id}': {c.type} {c.params}")
        for rule in self.signals.entry:
            side = rule.side or "either side"
            lines.append(f"  Entry '{rule.id}' ({side}, {rule.timeframe}) when ALL of:")
            for cond in rule.all_of:
                lines.append(f"      - {cond.type} {cond.params}")
            for cond in rule.any_of:
                lines.append(f"      - ANY: {cond.type} {cond.params}")
            for cond in rule.none_of:
                lines.append(f"      - NOT: {cond.type} {cond.params}")
        if self.risk.stop:
            lines.append(f"  Stop {self.risk.stop.value} {self.risk.stop.type}")
        if self.risk.target:
            lines.append(f"  Target {self.risk.target.value} {self.risk.target.type}")
        lines.append(
            f"  Size {self.risk.size.value} by {self.risk.size.method}; "
            f"max {self.risk.max_concurrent_positions} concurrent."
        )
        lines.append(
            f"  Costs: {self.costs.commission_per_side} per side, "
            f"{self.costs.slippage_ticks} ticks slippage, {self.costs.spread_ticks} ticks spread."
        )
        for w in self.filters.time_windows:
            lines.append(f"  Only between {w.start} and {w.end} {w.tz or self.universe.timezone}")
        if self.assumptions:
            lines.append("  Assumptions:")
            for a in self.assumptions:
                mark = "approved" if a.approved else "UNRESOLVED"
                lines.append(f"      [{a.id}] {a.question} -> {a.resolution} ({mark})")
        return "\n".join(lines)
