"""Versioning and diffing.

Natural language editing (ability 14) needs two things: a spec that can be
changed by one sentence, and a before/after the client can actually read. This
module is the second half.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from ee_agent.spec.model import StrategySpec


@dataclass
class Change:
    path: str
    before: Any
    after: Any

    @property
    def kind(self) -> str:
        if self.before is None:
            return "added"
        if self.after is None:
            return "removed"
        return "changed"

    def sentence(self) -> str:
        if self.kind == "added":
            return f"added {self.path} = {_short(self.after)}"
        if self.kind == "removed":
            return f"removed {self.path} (was {_short(self.before)})"
        return f"{self.path}: {_short(self.before)} -> {_short(self.after)}"


@dataclass
class SpecDiff:
    changes: list[Change]
    before_hash: str
    after_hash: str

    @property
    def empty(self) -> bool:
        return not self.changes

    def summary(self) -> str:
        if self.empty:
            return "No change: the spec is byte-identical."
        lines = [f"{len(self.changes)} change(s): {self.before_hash[:12]} -> {self.after_hash[:12]}"]
        lines += [f"  - {c.sentence()}" for c in self.changes]
        return "\n".join(lines)

    def unified(self, before: StrategySpec, after: StrategySpec) -> str:
        return "\n".join(
            difflib.unified_diff(
                before.to_yaml().splitlines(),
                after.to_yaml().splitlines(),
                fromfile=f"{before.id}@{before.short_hash}",
                tofile=f"{after.id}@{after.short_hash}",
                lineterm="",
            )
        )


def _short(v: Any) -> str:
    s = str(v)
    return s if len(s) <= 80 else s[:77] + "..."


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = v.get("id") if isinstance(v, dict) and "id" in v else i
            out.update(_flatten(v, f"{prefix}[{key}]"))
    else:
        out[prefix] = obj
    return out


def diff(before: StrategySpec, after: StrategySpec) -> SpecDiff:
    a, b = _flatten(before.to_dict()), _flatten(after.to_dict())
    changes: list[Change] = []
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key), b.get(key)
        if va != vb:
            changes.append(Change(key, va, vb))
    return SpecDiff(changes, before.hash, after.hash)


def bump(spec: StrategySpec, note: str = "") -> StrategySpec:
    """Return a copy with an incremented id suffix, preserving history in notes."""
    base, _, tail = spec.id.rpartition("-v")
    if base and tail.isdigit():
        new_id = f"{base}-v{int(tail) + 1}"
    else:
        new_id = f"{spec.id}-v2"
    out = StrategySpec.from_dict(spec.to_dict())
    out.id = new_id
    if note:
        out.notes = (out.notes + "\n" if out.notes else "") + f"[{spec.id} -> {new_id}] {note}"
    return out
