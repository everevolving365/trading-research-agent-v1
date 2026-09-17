"""Evaluating the LIVE config -- the fourth target, on its own path.

The execution layer must never import the spec object. It reads the live config
JSON, rebuilds the plan from it, and evaluates. That separation is what makes
the parity check meaningful: if the live config is missing something the spec
knew, the live fingerprint diverges and the harness catches it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ee_agent.spec import primitives as prim


@dataclass
class LiveSignals:
    long_entry: np.ndarray
    short_entry: np.ndarray
    long_exit: np.ndarray
    short_exit: np.ndarray
    invalidation: np.ndarray
    filter_ok: np.ndarray


class LivePlan:
    """Reconstructs the executable plan from a live config dictionary."""

    def __init__(self, config: dict):
        self.config = config
        self.spec_hash = config.get("spec_hash", "")
        self.plan_hash = config.get("plan_hash", "")
        self.spec_id = config.get("spec_id", "")
        self.timezone = config.get("timezone", "America/Chicago")
        self._env = prim.CompileEnv(spec=None)

    # --------------------------------------------------------------- build
    def _node(self, entry: dict, siblings: list[dict]):
        params = {k: v for k, v in entry.items() if k != "type"}
        env = prim.CompileEnv(spec=None)
        env.rule_conditions = [_AsCondition(s) for s in siblings]
        return prim.get(entry["type"]).python(params, env)

    def evaluate(self, bars, instrument=None) -> LiveSignals:
        cfg = self.config
        rt = prim.Runtime(bars, instrument=instrument, spec=None, strategy_tz=self.timezone)
        n = len(bars)

        for block in cfg.get("contexts", []):
            node = self._node(block, [])
            node.prepare(rt)

        def rule_series(rule: dict) -> np.ndarray:
            alls = rule.get("all_of", []) or []
            anys = rule.get("any_of", []) or []
            nones = rule.get("none_of", []) or []
            siblings = [*alls, *anys, *nones]
            out = np.ones(n, dtype=bool)
            for entry in alls:
                node = self._node(entry, siblings)
                node.prepare(rt)
                out &= node.series
            if anys:
                acc = np.zeros(n, dtype=bool)
                for entry in anys:
                    node = self._node(entry, siblings)
                    node.prepare(rt)
                    acc |= node.series
                out &= acc
            for entry in nones:
                node = self._node(entry, siblings)
                node.prepare(rt)
                out &= ~node.series
            return out

        filter_ok = np.ones(n, dtype=bool)
        for window in cfg.get("filters", {}).get("time_windows", []) or []:
            node = prim.get("time_window").python(dict(window), self._env)
            node.prepare(rt)
            filter_ok &= node.series
        days = cfg.get("filters", {}).get("days_of_week") or []
        if days:
            table = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
            filter_ok &= np.isin(bars.weekday, [table[d.lower()[:3]] for d in days])

        long_entry = np.zeros(n, dtype=bool)
        short_entry = np.zeros(n, dtype=bool)
        for rule in cfg.get("entries", []):
            fired = rule_series(rule) & filter_ok
            if rule.get("side") == "long":
                long_entry |= fired
            else:
                short_entry |= fired

        long_exit = np.zeros(n, dtype=bool)
        short_exit = np.zeros(n, dtype=bool)
        for rule in cfg.get("exits", []) or []:
            fired = rule_series(rule)
            if rule.get("side") == "long":
                long_exit |= fired
            else:
                short_exit |= fired

        invalidation = np.zeros(n, dtype=bool)
        invalid_entries = cfg.get("invalidation", []) or []
        for entry in invalid_entries:
            params = dict(entry)
            params.pop("type", None)
            if entry["type"] == "session_end" and "end" not in params:
                windows = cfg.get("filters", {}).get("time_windows") or []
                if windows:
                    params["end"] = windows[-1]["end"]
            node = prim.get(entry["type"]).python(params, self._env)
            node.prepare(rt)
            invalidation |= node.series

        return LiveSignals(long_entry, short_entry, long_exit, short_exit, invalidation, filter_ok)

    # ----------------------------------------------------------- integrity
    def verify_alert(self, payload: dict) -> tuple[bool, str]:
        """An alert from a stale or foreign script is refused before anything
        else happens. The hash in the payload must match this config."""
        if payload.get("spec_hash") != self.spec_hash:
            return False, (
                f"alert carries spec_hash {payload.get('spec_hash')!r}, this config is "
                f"{self.spec_hash!r}. The script on the chart is not the strategy this account runs."
            )
        if payload.get("plan_hash") and payload.get("plan_hash") != self.plan_hash:
            return False, "alert plan_hash does not match the compiled plan"
        if payload.get("side") not in ("long", "short"):
            return False, f"alert side {payload.get('side')!r} is not long or short"
        return True, ""


@dataclass
class _AsCondition:
    """Adapter so sibling-parameter inheritance works on plain dictionaries."""

    data: dict

    @property
    def type(self) -> str:
        return self.data.get("type", "")

    @property
    def params(self) -> dict:
        return {k: v for k, v in self.data.items() if k != "type"}
