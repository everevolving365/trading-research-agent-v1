"""Spec validation. Nothing ambiguous is allowed to survive into the spec.

Three severities:

* ``error``   -- the spec cannot compile. Blocks everything.
* ``blocking``-- the spec compiles but must not reach live capital or a reported
  result (an unapproved assumption, a missing cost model).
* ``warning`` -- worth saying out loud, does not stop anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ee_agent.errors import SpecError
from ee_agent.spec import primitives as prim
from ee_agent.spec.model import StrategySpec

Severity = Literal["error", "blocking", "warning"]


@dataclass
class Finding:
    severity: Severity
    code: str
    message: str
    where: str = ""

    def __str__(self) -> str:
        loc = f" [{self.where}]" if self.where else ""
        return f"{self.severity.upper():8} {self.code}{loc}: {self.message}"


@dataclass
class ValidationReport:
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def live_ready(self) -> bool:
        return not self.errors and not self.blocking

    def raise_if_bad(self) -> "ValidationReport":
        if self.errors:
            raise SpecError("\n".join(str(f) for f in self.errors))
        return self

    def __str__(self) -> str:
        if not self.findings:
            return "spec valid: no findings"
        return "\n".join(str(f) for f in self.findings)


#: The interrogation engine's completeness checklist (ability 10). Every item
#: must be answered by the client or logged as an approved assumption.
COMPLETENESS_CHECKLIST: list[tuple[str, str]] = [
    ("entry_trigger", "What exactly fires the entry?"),
    ("confirmation", "What confirms it before you act?"),
    ("invalidation", "What tells you the setup is dead?"),
    ("stop_placement", "Where does the stop go?"),
    ("target_logic", "Where do you take profit?"),
    ("session_filter", "Which sessions and hours does this trade?"),
    ("max_trades_per_day", "How many times a day will you take it?"),
    ("overlapping_signals", "What happens when two signals overlap?"),
    ("gap_behavior", "What happens when price gaps through your level?"),
    ("session_close_behavior", "What happens to an open position at session close?"),
    ("scale_in", "Does the strategy scale in?"),
]


def checklist_status(spec: StrategySpec) -> dict[str, bool]:
    """Which completeness items the spec answers. Used by the interrogation
    engine to decide what to ask next, and by the validator to block."""
    approved_ids = {a.id for a in spec.assumptions if a.approved}
    approved_topics = {a.id.split(":")[0] for a in spec.assumptions if a.approved}

    def covered(topic: str, answered: bool) -> bool:
        return answered or topic in approved_topics or topic in approved_ids

    return {
        "entry_trigger": covered("entry_trigger", bool(spec.signals.entry)),
        "confirmation": covered(
            "confirmation",
            any(len(r.all_of) + len(r.any_of) >= 2 for r in spec.signals.entry)
            or spec.execution.bar_close_confirmation,
        ),
        "invalidation": covered(
            "invalidation", bool(spec.signals.invalidation) or bool(spec.signals.exit)
        ),
        "stop_placement": covered("stop_placement", spec.risk.stop is not None),
        "target_logic": covered(
            "target_logic", spec.risk.target is not None or spec.risk.trail is not None
        ),
        "session_filter": covered(
            "session_filter", bool(spec.filters.time_windows) or spec.universe.session != "custom"
        ),
        "max_trades_per_day": covered("max_trades_per_day", spec.filters.max_trades_per_day is not None),
        "overlapping_signals": covered("overlapping_signals", bool(spec.execution.on_overlapping_signal)),
        "gap_behavior": covered("gap_behavior", bool(spec.execution.gap_behavior)),
        "session_close_behavior": covered(
            "session_close_behavior", bool(spec.execution.session_close_behavior)
        ),
        "scale_in": covered("scale_in", spec.risk.scale_in is not None),
    }


def validate(spec: StrategySpec, *, for_live: bool = False) -> ValidationReport:
    f: list[Finding] = []

    # ---- identity ------------------------------------------------------
    if not spec.id or " " in spec.id:
        f.append(Finding("error", "SPEC001", "id must be present and contain no spaces", "id"))
    if spec.spec_version != 1:
        f.append(Finding("warning", "SPEC002", f"spec_version {spec.spec_version} is not 1", "spec_version"))

    # ---- universe ------------------------------------------------------
    if not spec.universe.instruments:
        f.append(Finding("error", "SPEC010", "universe.instruments is empty", "universe"))
    if not spec.universe.timezone:
        f.append(Finding("error", "SPEC011", "universe.timezone is required", "universe"))

    # ---- primitives: all four targets must exist -----------------------
    for problem in prim.audit_registry():
        f.append(Finding("error", "PRIM000", f"registry violates the four-target rule: {problem}"))

    context_ids = {c.id for c in spec.context}
    for c in spec.context:
        if c.type not in prim.REGISTRY:
            f.append(Finding("error", "PRIM001", f"unknown context primitive '{c.type}'", f"context/{c.id}"))
        elif prim.REGISTRY[c.type].kind != "context":
            f.append(Finding("error", "PRIM002", f"'{c.type}' is not a context primitive", f"context/{c.id}"))

    for rule in [*spec.signals.entry, *spec.signals.exit]:
        if not (rule.all_of or rule.any_of):
            f.append(Finding("error", "SIG001", "rule has no conditions", f"signal/{rule.id}"))
        side = rule.side or (rule.id if rule.id in ("long", "short") else None)
        if side not in ("long", "short"):
            f.append(
                Finding(
                    "error",
                    "SIG002",
                    "rule needs side: long|short (or an id of exactly 'long'/'short')",
                    f"signal/{rule.id}",
                )
            )
        for cond in rule.conditions:
            _check_condition(cond, context_ids, f, f"signal/{rule.id}")

    for cond in spec.signals.invalidation:
        _check_condition(cond, context_ids, f, "invalidation")

    # ---- risk ----------------------------------------------------------
    if spec.risk.stop is None:
        f.append(Finding("blocking", "RISK001", "no stop defined: the client must state one", "risk.stop"))
    if spec.risk.target is None and spec.risk.trail is None and not spec.signals.exit:
        f.append(
            Finding(
                "blocking",
                "RISK002",
                "no target, trail or exit rule: nothing closes a winning trade",
                "risk",
            )
        )
    for label, dist in (("stop", spec.risk.stop), ("target", spec.risk.target), ("trail", spec.risk.trail)):
        if dist is None:
            continue
        if dist.type not in ("ticks", "points", "percent", "atr", "stdev", "bps"):
            f.append(
                Finding(
                    "error",
                    "RISK010",
                    f"{label} unit '{dist.type}' is not dimensionless; "
                    "use ticks, points, percent, atr, stdev or bps -- never currency",
                    f"risk.{label}",
                )
            )
        if dist.value <= 0:
            f.append(Finding("error", "RISK011", f"{label} value must be positive", f"risk.{label}"))
    if spec.risk.max_concurrent_positions < 1:
        f.append(Finding("error", "RISK012", "max_concurrent_positions must be >= 1", "risk"))

    # ---- portability across the declared universe -----------------------
    from ee_agent.spec.portability import portability_note

    note = portability_note(spec)
    if note and len(spec.universe.instruments) > 1:
        asset_classes = set()
        for symbol in spec.universe.instruments:
            try:
                from ee_agent.instruments.registry import get_instrument

                asset_classes.add(get_instrument(symbol).asset_class)
            except KeyError:
                continue
        severity: Severity = "blocking" if len(asset_classes) > 1 else "warning"
        f.append(Finding(severity, "PORT001", note, "risk"))

    # ---- costs: hard rule 7 --------------------------------------------
    if spec.costs.commission_per_side == 0 and spec.costs.slippage_ticks == 0 and spec.costs.spread_ticks == 0:
        f.append(
            Finding(
                "blocking",
                "COST001",
                "zero commission, zero slippage and zero spread: a frictionless "
                "backtest is not a defensible number (Law One)",
                "costs",
            )
        )

    # ---- execution -----------------------------------------------------
    if spec.execution.on_overlapping_signal not in ("ignore_new", "replace", "stack"):
        f.append(Finding("error", "EXE001", "on_overlapping_signal must be ignore_new|replace|stack", "execution"))
    if spec.execution.session_close_behavior not in ("flatten", "hold", "flatten_at"):
        f.append(Finding("error", "EXE002", "session_close_behavior must be flatten|hold|flatten_at", "execution"))

    # ---- ambiguity ledger ----------------------------------------------
    for a in spec.unresolved_assumptions():
        f.append(
            Finding(
                "blocking",
                "AMB001",
                f"assumption '{a.id}' is unresolved: {a.question}",
                "assumptions",
            )
        )

    # ---- completeness checklist ----------------------------------------
    status = checklist_status(spec)
    for key, question in COMPLETENESS_CHECKLIST:
        if not status.get(key, False):
            severity: Severity = "blocking" if for_live else "warning"
            f.append(Finding(severity, "CHK001", f"unanswered: {question}", f"checklist/{key}"))

    return ValidationReport(f)


def _check_condition(cond, context_ids: set[str], f: list[Finding], where: str) -> None:
    if cond.type not in prim.REGISTRY:
        f.append(Finding("error", "PRIM003", f"unknown condition primitive '{cond.type}'", where))
        return
    p = prim.REGISTRY[cond.type]
    if p.kind != "condition":
        f.append(Finding("error", "PRIM004", f"'{cond.type}' is a context, not a condition", where))
    ref = cond.params.get("reference")
    if ref and ref not in context_ids and ref not in ("signal_window", "session"):
        f.append(Finding("error", "PRIM005", f"'{cond.type}' references unknown context '{ref}'", where))
