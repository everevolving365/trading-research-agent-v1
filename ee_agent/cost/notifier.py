"""The cost layer. It estimates, it informs, it proceeds. It never blocks.

Hard rule 9. Section 7 of the build README: "the agent informs, it never
refuses". The only stop in this module is a ceiling the *client* set for
themselves, and even that asks rather than silently halting.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ee_agent.paths import ee_home

# --------------------------------------------------------------- price sheet
#: USD unit costs. Deliberately data, not code: a client can edit these to match
#: their own contracts without touching the estimator.
PRICE_SHEET: dict[str, float] = {
    # model tokens, USD per 1k
    "model.input_1k": 0.003,
    "model.output_1k": 0.015,
    "vision.image": 0.004,  # one screenshot, one vision call
    "vision.image_downscaled": 0.0015,  # ability 76: downscaled screenshots
    # data
    "data.bar_1k": 0.0,  # keyless crypto / cached / fixtures
    "data.bar_1k_paid": 0.02,
    "data.tick_1m_rows": 1.20,  # tick data: orders of magnitude above bars
    "data.orderflow_day": 0.35,
    # compute
    "cpu.core_minute": 0.004,
    "storage.gb_month": 0.023,
    # subscriptions, reported for transparency, charged by the vendor not us
    "sub.tradingview_premium_month": 59.95,
    "sub.prop_eval_reset": 149.0,
}

OP_LABELS: dict[str, str] = {
    "conversation": "talking with you",
    "interrogation": "questioning the strategy until it is unambiguous",
    "visual_confirmation": "rendering annotated charts for your approval",
    "operator_session": "driving your browser on TradingView",
    "data_fetch": "fetching market data",
    "backtest": "running the Python backtest",
    "walk_forward": "walk-forward analysis",
    "monte_carlo": "Monte Carlo drawdown distribution",
    "synthetic": "synthetic market bootstrap",
    "scanner": "cross-asset scan",
    "overnight": "the overnight research loop",
    "adversarial": "the adversarial pass (the case against the result)",
}


@dataclass
class CostEstimate:
    operation: str
    usd: float
    breakdown: dict[str, float] = field(default_factory=dict)
    note: str = ""

    def explain(self) -> str:
        what = OP_LABELS.get(self.operation, self.operation)
        parts = ", ".join(f"{k} ${v:,.4f}" for k, v in self.breakdown.items() if v)
        line = f"[cost] {what}: about ${self.usd:,.4f}"
        if parts:
            line += f"  ({parts})"
        if self.note:
            line += f"\n       {self.note}"
        return line


@dataclass
class CostRecord:
    ts: str
    operation: str
    estimated_usd: float
    actual_usd: float
    detail: dict = field(default_factory=dict)


def estimate(operation: str, **units: float) -> CostEstimate:
    """Estimate an operation from unit counts, e.g. ``estimate("backtest",
    **{"cpu.core_minute": 0.5})``."""
    breakdown = {}
    total = 0.0
    for unit, count in units.items():
        price = PRICE_SHEET.get(unit)
        if price is None:
            continue
        cost = price * float(count)
        breakdown[unit] = cost
        total += cost
    return CostEstimate(operation=operation, usd=round(total, 6), breakdown=breakdown)


class CeilingReached(Exception):
    """The client's own optional ceiling was reached. The agent asks, it does not stop."""

    def __init__(self, spent: float, ceiling: float, operation: str):
        super().__init__(
            f"You set a ceiling of ${ceiling:,.2f} and this session has spent ${spent:,.2f}. "
            f"The next operation is {OP_LABELS.get(operation, operation)}. "
            f"Raise the ceiling or say go ahead -- I have not stopped anything yet."
        )
        self.spent, self.ceiling, self.operation = spent, ceiling, operation


class CostLedger:
    """Running total, visible at all times, recorded next to every result."""

    def __init__(
        self,
        ceiling_usd: float | None = None,
        notify_threshold_usd: float = 0.10,
        sink: Callable[[str], None] | None = None,
        path: Path | None = None,
    ) -> None:
        env_ceiling = os.environ.get("EE_COST_CEILING_USD")
        if ceiling_usd is None and env_ceiling:
            try:
                ceiling_usd = float(env_ceiling)
            except ValueError:
                ceiling_usd = None
        self.ceiling_usd = ceiling_usd
        self.notify_threshold_usd = notify_threshold_usd
        self.sink = sink or (lambda msg: print(msg))
        self.path = path or (ee_home() / "cost-ledger.jsonl")
        self.records: list[CostRecord] = []
        self._lock = threading.Lock()
        #: set by the client saying "go ahead" after a ceiling prompt
        self.ceiling_acknowledged = False

    # ------------------------------------------------------------------ total
    @property
    def total_usd(self) -> float:
        return round(sum(r.actual_usd for r in self.records), 6)

    def banner(self) -> str:
        line = f"[spend] session total ${self.total_usd:,.4f}"
        if self.ceiling_usd:
            line += f" of your ${self.ceiling_usd:,.2f} ceiling"
        return line

    # ------------------------------------------------------- inform + proceed
    def announce(self, est: CostEstimate) -> CostEstimate:
        """Tell the client what this will cost, then return. NEVER raises on cost
        grounds except for a ceiling the client themselves set."""
        if est.usd >= self.notify_threshold_usd:
            self.sink(est.explain())
            self.sink(self.banner())
        if (
            self.ceiling_usd is not None
            and not self.ceiling_acknowledged
            and self.total_usd + est.usd > self.ceiling_usd
        ):
            raise CeilingReached(self.total_usd, self.ceiling_usd, est.operation)
        return est

    def record(self, operation: str, estimated_usd: float, actual_usd: float | None = None, **detail) -> CostRecord:
        rec = CostRecord(
            ts=datetime.now(timezone.utc).isoformat(),
            operation=operation,
            estimated_usd=round(estimated_usd, 6),
            actual_usd=round(estimated_usd if actual_usd is None else actual_usd, 6),
            detail=detail,
        )
        with self._lock:
            self.records.append(rec)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(rec)) + "\n")
            except OSError:  # pragma: no cover - never let bookkeeping kill a run
                pass
        return rec

    def spend(self, operation: str, **units: float) -> CostRecord:
        """Estimate, announce, record. The one call most modules make."""
        est = self.announce(estimate(operation, **units))
        return self.record(operation, est.usd, detail=est.breakdown)

    def acknowledge_ceiling(self) -> None:
        """The client said go ahead after being asked. Never assumed."""
        self.ceiling_acknowledged = True


_LEDGER: CostLedger | None = None


def ledger() -> CostLedger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = CostLedger()
    return _LEDGER


def set_ledger(new: CostLedger) -> None:
    global _LEDGER
    _LEDGER = new
