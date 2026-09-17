"""Compile a spec to an executable Python plan.

This path contains **no model call**. Compilation is deterministic: the same
spec always produces the same plan, the same signals and the same artifact hash
(Phase 1 and Phase 5 acceptance). A test asserts the absence of any model client
import in this module's import graph.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ee_agent.spec import primitives as prim
from ee_agent.spec.model import SignalRule, StrategySpec


@dataclass
class SignalArrays:
    """Causal boolean arrays, one entry per bar."""

    long_entry: np.ndarray
    short_entry: np.ndarray
    long_exit: np.ndarray
    short_exit: np.ndarray
    invalidation: np.ndarray
    filter_ok: np.ndarray
    debug: dict[str, np.ndarray] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "long_entry": int(self.long_entry.sum()),
            "short_entry": int(self.short_entry.sum()),
            "long_exit": int(self.long_exit.sum()),
            "short_exit": int(self.short_exit.sum()),
            "invalidation": int(self.invalidation.sum()),
        }


@dataclass
class CompiledRule:
    id: str
    side: str
    rule: SignalRule
    all_nodes: list[prim.Node]
    any_nodes: list[prim.Node]
    none_nodes: list[prim.Node]

    def evaluate(self, rt: prim.Runtime) -> np.ndarray:
        n = rt.n
        out = np.ones(n, dtype=bool)
        for node in self.all_nodes:
            out &= node.series
        if self.any_nodes:
            anyv = np.zeros(n, dtype=bool)
            for node in self.any_nodes:
                anyv |= node.series
            out &= anyv
        for node in self.none_nodes:
            out &= ~node.series
        return out


class CompiledStrategy:
    """The Python target. Holds the compiled nodes and evaluates them causally."""

    def __init__(self, spec: StrategySpec):
        self.spec = spec
        self.env = prim.CompileEnv(spec=spec)
        self.context_nodes: list[prim.Node] = []
        self.entry_rules: list[CompiledRule] = []
        self.exit_rules: list[CompiledRule] = []
        self.invalidation_nodes: list[prim.Node] = []
        self.filter_nodes: list[prim.Node] = []
        self._build()

    # ---------------------------------------------------------------- build
    def _build(self) -> None:
        spec = self.spec
        for block in spec.context:
            p = prim.get(block.type)
            self.context_nodes.append(p.python({"id": block.id, **block.params}, self.env))

        for rule in spec.signals.entry:
            self.entry_rules.append(self._compile_rule(rule))
        for rule in spec.signals.exit:
            self.exit_rules.append(self._compile_rule(rule))

        for cond in spec.signals.invalidation:
            env = prim.CompileEnv(spec=spec, rule_conditions=spec.signals.invalidation)
            self.invalidation_nodes.append(prim.get(cond.type).python(dict(cond.params), env))

        for window in spec.filters.time_windows:
            params = {"start": window.start, "end": window.end, "tz": window.tz or spec.universe.timezone}
            self.filter_nodes.append(prim.get("time_window").python(params, self.env))

    def _compile_rule(self, rule: SignalRule) -> CompiledRule:
        side = rule.side or (rule.id if rule.id in ("long", "short") else "long")
        env = prim.CompileEnv(spec=self.spec, rule_side=side, rule_conditions=rule.conditions)

        def build(conds):
            return [prim.get(c.type).python(dict(c.params), env) for c in conds]

        return CompiledRule(
            id=rule.id,
            side=side,
            rule=rule,
            all_nodes=build(rule.all_of),
            any_nodes=build(rule.any_of),
            none_nodes=build(rule.none_of),
        )

    # ------------------------------------------------------------- evaluate
    def evaluate(self, bars, instrument=None) -> SignalArrays:
        rt = prim.Runtime(bars, instrument=instrument, spec=self.spec)
        for node in self.context_nodes:
            node.prepare(rt)
        for rule in [*self.entry_rules, *self.exit_rules]:
            for node in [*rule.all_nodes, *rule.any_nodes, *rule.none_nodes]:
                node.prepare(rt)
        for node in [*self.invalidation_nodes, *self.filter_nodes]:
            node.prepare(rt)

        n = len(bars)
        filter_ok = np.ones(n, dtype=bool)
        for node in self.filter_nodes:
            filter_ok &= node.series

        if self.spec.filters.days_of_week:
            wanted = {
                {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}[d.lower()[:3]]
                for d in self.spec.filters.days_of_week
            }
            filter_ok &= np.isin(bars.weekday, list(wanted))

        long_entry = np.zeros(n, dtype=bool)
        short_entry = np.zeros(n, dtype=bool)
        debug: dict[str, np.ndarray] = {}
        for rule in self.entry_rules:
            fired = rule.evaluate(rt) & filter_ok
            debug[f"entry:{rule.id}"] = fired
            if rule.side == "long":
                long_entry |= fired
            else:
                short_entry |= fired

        long_exit = np.zeros(n, dtype=bool)
        short_exit = np.zeros(n, dtype=bool)
        for rule in self.exit_rules:
            fired = rule.evaluate(rt)
            debug[f"exit:{rule.id}"] = fired
            if rule.side == "long":
                long_exit |= fired
            else:
                short_exit |= fired

        invalidation = np.zeros(n, dtype=bool)
        for node in self.invalidation_nodes:
            invalidation |= node.series

        for key, arr in rt.scratch.items():
            if isinstance(arr, np.ndarray) and len(arr) == n:
                debug[f"scratch:{key}"] = arr
        for cid, arrays in rt.contexts.items():
            for key, arr in arrays.items():
                debug[f"context:{cid}.{key}"] = arr

        return SignalArrays(
            long_entry=long_entry,
            short_entry=short_entry,
            long_exit=long_exit,
            short_exit=short_exit,
            invalidation=invalidation,
            filter_ok=filter_ok,
            debug=debug,
        )

    # ------------------------------------------------------------- identity
    @property
    def plan_hash(self) -> str:
        return "sha256:" + hashlib.sha256(_plan_blob(self.spec).encode("utf-8")).hexdigest()


def _plan_blob(spec: StrategySpec) -> str:
    """A canonical description of the compiled plan -- what the four targets
    must agree on. Used by the parity harness."""
    import json

    parts: dict[str, Any] = {
        "contexts": [c.to_dict() for c in spec.context],
        "entries": [r.to_dict() for r in spec.signals.entry],
        "exits": [r.to_dict() for r in spec.signals.exit],
        "invalidation": [c.to_dict() for c in spec.signals.invalidation],
        "filters": [
            {"start": w.start, "end": w.end, "tz": w.tz or spec.universe.timezone}
            for w in spec.filters.time_windows
        ],
        "risk": spec.risk.to_dict(),
        "execution": {
            "entry_order": spec.execution.entry_order,
            "session_close_behavior": spec.execution.session_close_behavior,
            "on_overlapping_signal": spec.execution.on_overlapping_signal,
            "gap_behavior": spec.execution.gap_behavior,
            "bar_close_confirmation": spec.execution.bar_close_confirmation,
        },
        "costs": spec.costs.to_dict(),
    }
    return json.dumps(parts, sort_keys=True, separators=(",", ":"))


def compile_to_python(spec: StrategySpec) -> CompiledStrategy:
    return CompiledStrategy(spec)


SCRIPT_TEMPLATE = '''"""GENERATED -- do not edit.

Compiled from strategy spec: {spec_id}
Spec hash: {spec_hash}
Plan hash: {plan_hash}

This file is deterministic output of ee_agent.compile.to_python. It contains no
model-written code. Regenerating it from the same spec produces the same bytes.
"""
from __future__ import annotations

import json
import sys

from ee_agent.spec.model import StrategySpec
from ee_agent.compile.to_python import compile_to_python
from ee_agent.engine.backtester import Backtester
from ee_agent.data.loader import load_bars

SPEC_YAML = r"""
{spec_yaml}
"""


def main(symbol: str = "{default_symbol}", timeframe: str = "{timeframe}") -> dict:
    spec = StrategySpec.from_yaml(SPEC_YAML)
    bars = load_bars(symbol, timeframe, lookback_days=365)
    print(bars.age_line())
    result = Backtester(spec).run(bars)
    print(result.report())
    return result.to_dict()


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "{default_symbol}"
    out = main(symbol)
    print(json.dumps({{"spec": "{spec_id}", "spec_hash": "{spec_hash}", "metrics": out["metrics"]}}, indent=2))
'''


def emit_script(spec: StrategySpec) -> str:
    """A standalone, reviewable Python file. Law Three: generated code is
    displayed before it executes, and executes in the sandbox."""
    compiled = compile_to_python(spec)
    tf = spec.signals.entry[0].timeframe if spec.signals.entry else "5m"
    return SCRIPT_TEMPLATE.format(
        spec_id=spec.id,
        spec_hash=spec.hash,
        plan_hash=compiled.plan_hash,
        spec_yaml=spec.to_yaml().strip(),
        default_symbol=spec.universe.instruments[0] if spec.universe.instruments else "BTCUSDT",
        timeframe=tf,
    )
