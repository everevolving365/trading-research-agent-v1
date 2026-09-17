"""Compile a spec to a live execution config -- the fourth target.

The live config is what the execution layer reads. It carries the same plan hash
as the Python backtest and the two Pine artifacts, which is how the one-sentence
test is answered: the strategy trading the account and the strategy the client
described are the same object, and the hash proves it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any

from ee_agent.spec import primitives as prim
from ee_agent.spec.model import StrategySpec


@dataclass
class LiveConfig:
    spec_id: str
    spec_hash: str
    plan_hash: str
    instruments: list[str]
    timezone: str
    session: str
    contexts: list[dict] = field(default_factory=list)
    entries: list[dict] = field(default_factory=list)
    exits: list[dict] = field(default_factory=list)
    invalidation: list[dict] = field(default_factory=list)
    filters: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    execution: dict = field(default_factory=dict)
    costs: dict = field(default_factory=dict)
    alerts: dict = field(default_factory=dict)
    autonomy: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @property
    def hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def save(self, path) -> None:
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(self.to_json(), encoding="utf-8")

    def webhook_payload_template(self, side: str) -> dict[str, Any]:
        """What the TradingView alert should POST. The receiver verifies
        ``spec_hash`` before acting: an alert from a stale script is refused."""
        return {
            "spec_id": self.spec_id,
            "spec_hash": self.spec_hash,
            "plan_hash": self.plan_hash,
            "side": side,
            "symbol": "{{ticker}}",
            "price": "{{close}}",
            "time": "{{timenow}}",
            "signature": "<hmac-sha256 of the fields above, keyed by the client's webhook secret>",
        }


def compile_to_live(spec: StrategySpec, autonomy_level: int = 1) -> LiveConfig:
    from ee_agent.compile.to_python import CompiledStrategy

    env = prim.CompileEnv(spec=spec)
    contexts = [prim.get(c.type).live({"id": c.id, **c.params}, env) for c in spec.context]

    def rule_dict(rule) -> dict:
        side = rule.side or (rule.id if rule.id in ("long", "short") else "long")
        renv = prim.CompileEnv(spec=spec, rule_side=side, rule_conditions=rule.conditions)
        return {
            "id": rule.id,
            "side": side,
            "timeframe": rule.timeframe,
            "all_of": [prim.get(c.type).live(dict(c.params), renv) for c in rule.all_of],
            "any_of": [prim.get(c.type).live(dict(c.params), renv) for c in rule.any_of],
            "none_of": [prim.get(c.type).live(dict(c.params), renv) for c in rule.none_of],
        }

    cfg = LiveConfig(
        spec_id=spec.id,
        spec_hash=spec.hash,
        plan_hash=CompiledStrategy(spec).plan_hash,
        instruments=list(spec.universe.instruments),
        timezone=spec.universe.timezone,
        session=spec.universe.session,
        contexts=contexts,
        entries=[rule_dict(r) for r in spec.signals.entry],
        exits=[rule_dict(r) for r in spec.signals.exit],
        invalidation=[
            prim.get(c.type).live(dict(c.params), prim.CompileEnv(spec=spec, rule_conditions=spec.signals.invalidation))
            for c in spec.signals.invalidation
        ],
        filters={
            "time_windows": [
                {"start": w.start, "end": w.end, "tz": w.tz or spec.universe.timezone}
                for w in spec.filters.time_windows
            ],
            "max_trades_per_day": spec.filters.max_trades_per_day,
            "days_of_week": spec.filters.days_of_week,
            "blackout_events": spec.filters.blackout_events,
        },
        risk=spec.risk.to_dict(),
        execution={
            "entry_order": spec.execution.entry_order,
            "session_close_behavior": spec.execution.session_close_behavior,
            "on_overlapping_signal": spec.execution.on_overlapping_signal,
            "gap_behavior": spec.execution.gap_behavior,
            "bar_close_confirmation": spec.execution.bar_close_confirmation,
        },
        costs=spec.costs.to_dict(),
        autonomy={"level": autonomy_level},
    )
    cfg.alerts = {
        "long": cfg.webhook_payload_template("long"),
        "short": cfg.webhook_payload_template("short"),
    }
    return cfg
