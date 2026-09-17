"""Overnight research, the hypothesis queue and the cross-asset scanner
(abilities 62, 63, 64).

The overnight loop generates its own variants of the *client's* strategy, tests
them out of sample and against synthetic data, runs the adversarial pass, and
reports only survivors in the morning.

It never invents a strategy. Every variant is a perturbation of parameters the
client already chose, and the selection correction knows exactly how many were
tried -- which is the difference between research and data dredging.
"""
from __future__ import annotations

import json
import itertools
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ee_agent.cost.notifier import CostEstimate, estimate, ledger
from ee_agent.paths import ee_home
from ee_agent.spec.model import Distance, StrategySpec


# ============================================================ hypothesis queue
@dataclass
class Hypothesis:
    id: str
    text: str
    created: str
    status: str = "queued"  # queued | running | done | failed
    spec_id: str = ""
    result: dict | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class HypothesisQueue:
    """The client speaks an idea at any hour; it queues and gets worked (63)."""

    def __init__(self, path: Path | None = None):
        self.path = path or (ee_home() / "hypotheses.jsonl")

    def all(self) -> list[Hypothesis]:
        if not self.path.exists():
            return []
        return [
            Hypothesis(**json.loads(line))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def add(self, text: str, spec_id: str = "") -> Hypothesis:
        items = self.all()
        h = Hypothesis(
            id=f"H-{len(items) + 1:04d}",
            text=text.strip(),
            created=datetime.now(timezone.utc).isoformat(),
            spec_id=spec_id,
        )
        self._write(items + [h])
        return h

    def pending(self) -> list[Hypothesis]:
        return [h for h in self.all() if h.status == "queued"]

    def complete(self, hypothesis_id: str, result: dict, note: str = "") -> None:
        items = self.all()
        for h in items:
            if h.id == hypothesis_id:
                h.status = "done"
                h.result = result
                h.note = note
        self._write(items)

    def _write(self, items: list[Hypothesis]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fh:
            for h in items:
                fh.write(json.dumps(h.to_dict()) + "\n")

    def summary(self) -> str:
        items = self.all()
        if not items:
            return "[hypotheses] queue is empty."
        pending = [h for h in items if h.status == "queued"]
        lines = [f"[hypotheses] {len(items)} total, {len(pending)} waiting to be worked:"]
        for h in items[-8:]:
            lines.append(f"    {h.id} [{h.status:<7}] {h.text[:80]}")
        return "\n".join(lines)


# =============================================================== variants
def generate_variants(spec: StrategySpec, max_variants: int = 24) -> list[tuple[str, StrategySpec]]:
    """Perturbations of what the client already chose. Never a new strategy.

    Returns (description, spec) pairs. The count matters: it is fed straight
    into the multiple-comparison correction so the winner's number is deflated
    by exactly how many were tried.
    """
    out: list[tuple[str, StrategySpec]] = []
    stop_multipliers = [0.75, 1.0, 1.5]
    target_multipliers = [0.75, 1.0, 1.5, 2.0]
    window_shifts = [0, 30]

    for sm, tm, shift in itertools.product(stop_multipliers, target_multipliers, window_shifts):
        if sm == 1.0 and tm == 1.0 and shift == 0:
            continue  # that is the original
        variant = StrategySpec.from_dict(spec.to_dict())
        parts = []
        if variant.risk.stop and sm != 1.0:
            variant.risk.stop = Distance(variant.risk.stop.type, round(variant.risk.stop.value * sm, 4))
            parts.append(f"stop x{sm}")
        if variant.risk.target and tm != 1.0:
            variant.risk.target = Distance(variant.risk.target.type, round(variant.risk.target.value * tm, 4))
            parts.append(f"target x{tm}")
        if shift and variant.filters.time_windows:
            window = variant.filters.time_windows[0]
            start_minutes = int(window.start[:2]) * 60 + int(window.start[3:]) + shift
            variant.filters.time_windows[0].start = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
            parts.append(f"start +{shift}m")
        if not parts:
            continue
        variant.id = f"{spec.id}-v{len(out) + 1}"
        out.append((", ".join(parts), variant))
        if len(out) >= max_variants:
            break
    return out


# ============================================================== overnight loop
@dataclass
class VariantResult:
    description: str
    spec_id: str
    net_pnl: float
    oos_net_pnl: float
    sharpe: float
    n_trades: int
    survived: bool
    verdict: str
    synthetic_percentile: float | None = None
    combine_pass_rate: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OvernightReport:
    started: str
    finished: str
    spec_id: str
    symbol: str
    n_variants: int
    survivors: list[VariantResult] = field(default_factory=list)
    rejected: list[VariantResult] = field(default_factory=list)
    cost_usd: float = 0.0
    corrected_sharpe: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started": self.started,
            "finished": self.finished,
            "spec_id": self.spec_id,
            "symbol": self.symbol,
            "n_variants": self.n_variants,
            "n_survivors": len(self.survivors),
            "survivors": [s.to_dict() for s in self.survivors],
            "rejected": [r.to_dict() for r in self.rejected],
            "cost_usd": round(self.cost_usd, 4),
            "corrected_sharpe": self.corrected_sharpe,
            "notes": self.notes,
        }

    def morning_brief(self) -> str:
        """What the client hears in the morning. Survivors only (ability 62)."""
        lines = [
            f"Overnight on {self.symbol}: I tested {self.n_variants} variants of your "
            f"{self.spec_id} and {len(self.survivors)} survived.",
        ]
        if not self.survivors:
            lines.append(
                "  None of them. That is a result, not a failure -- it means the parameters you "
                "already chose are not obviously improvable by moving them around."
            )
        for s in self.survivors[:5]:
            lines.append(
                f"  {s.description:<28} net {s.net_pnl:>10,.0f}  out-of-sample {s.oos_net_pnl:>10,.0f}  "
                f"{s.n_trades} trades"
            )
            lines.append(f"      {s.verdict}")
        if self.corrected_sharpe is not None:
            lines.append(
                f"  Corrected for having tested {self.n_variants} variants, the best Sharpe is "
                f"{self.corrected_sharpe:.2f}. That is the number to believe."
            )
        lines.append(f"  This cost ${self.cost_usd:,.2f} to run.")
        for note in self.notes:
            lines.append(f"  {note}")
        return "\n".join(lines)


def estimate_overnight(spec: StrategySpec, bars, n_variants: int, mc_paths: int, synth_paths: int) -> CostEstimate:
    """Declared BEFORE the loop starts (Phase 10 acceptance)."""
    bar_count = len(bars)
    core_minutes = (
        n_variants * bar_count * (1 + 5 * 2 + synth_paths) / 200_000.0 + n_variants * mc_paths / 20_000.0
    )
    est = estimate(
        "overnight",
        **{
            "cpu.core_minute": core_minutes,
            "model.input_1k": n_variants * 2.0,
            "model.output_1k": n_variants * 0.8,
        },
    )
    est.note = (
        f"{n_variants} variants x ({bar_count:,} bars, 5 walk-forward windows, {mc_paths} Monte Carlo "
        f"paths, {synth_paths} synthetic histories). This is the combinatorial cost Section 7 warns "
        "about: it grows with variants x windows x paths."
    )
    return est


def run_overnight(
    spec: StrategySpec,
    bars,
    max_variants: int = 12,
    monte_carlo_paths: int = 500,
    synthetic_paths: int = 4,
    announce: bool = True,
) -> OvernightReport:
    """Generate, test out of sample and against synthetic data, attack, report
    only survivors."""
    from ee_agent.engine.analysis import correct_for_selection
    from ee_agent.engine.truth import TruthEngine

    started = datetime.now(timezone.utc).isoformat()
    variants = generate_variants(spec, max_variants=max_variants)
    n_total = len(variants) + 1  # the original counts as a tested hypothesis

    est = estimate_overnight(spec, bars, n_total, monte_carlo_paths, synthetic_paths)
    if announce:
        ledger().announce(est)
    cost_before = ledger().total_usd

    report = OvernightReport(
        started=started,
        finished="",
        spec_id=spec.id,
        symbol=bars.symbol,
        n_variants=n_total,
    )

    results: list[tuple[VariantResult, float]] = []
    for description, variant in [("original (unchanged)", spec), *variants]:
        try:
            engine = TruthEngine(
                variant,
                monte_carlo_paths=monte_carlo_paths,
                synthetic_paths=synthetic_paths,
                n_variants_tested=n_total,
            )
            analysis = engine.analyze(bars, pin=False)
        except Exception as exc:
            report.notes.append(f"{description}: failed to run ({exc})")
            continue
        oos = analysis.walk_forward.out_of_sample if analysis.walk_forward else None
        entry = VariantResult(
            description=description,
            spec_id=variant.id,
            net_pnl=analysis.base.metrics.net_pnl,
            oos_net_pnl=oos.net_pnl if oos else 0.0,
            sharpe=analysis.base.metrics.sharpe,
            n_trades=analysis.base.metrics.n_trades,
            survived=bool(analysis.adversarial and analysis.adversarial.survived and (oos and oos.net_pnl > 0)),
            verdict=analysis.adversarial.verdict if analysis.adversarial else "",
            synthetic_percentile=analysis.synthetic.percentile_of_real if analysis.synthetic else None,
            combine_pass_rate=analysis.combine.pass_rate if analysis.combine else None,
        )
        results.append((entry, analysis.base.metrics.sharpe))
        (report.survivors if entry.survived else report.rejected).append(entry)

    report.survivors.sort(key=lambda r: -r.oos_net_pnl)
    if results:
        best_sharpe = max(sharpe for _entry, sharpe in results)
        n_obs = max((e.n_trades for e, _s in results), default=2)
        report.corrected_sharpe = round(
            correct_for_selection(best_sharpe, n_total, max(n_obs, 2)).corrected_metric, 4
        )
    report.cost_usd = ledger().total_usd - cost_before
    report.finished = datetime.now(timezone.utc).isoformat()
    _save_overnight(report)
    return report


def _save_overnight(report: OvernightReport) -> Path:
    path = ee_home() / "overnight" / f"{report.spec_id}-{report.started[:10]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return path


# ============================================================ cross-asset scan
@dataclass
class ScanRow:
    symbol: str
    net_pnl: float
    oos_net_pnl: float
    n_trades: int
    sharpe: float
    max_drawdown: float
    survived: bool
    verdict: str
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanReport:
    spec_id: str
    rows: list[ScanRow] = field(default_factory=list)
    cost_usd: float = 0.0
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def strongest(self) -> list[ScanRow]:
        return sorted([r for r in self.rows if r.survived], key=lambda r: -r.oos_net_pnl)

    @property
    def inverted(self) -> list[ScanRow]:
        """Where the edge runs backwards -- often the most interesting result."""
        return sorted([r for r in self.rows if r.net_pnl < 0 and r.n_trades >= 20], key=lambda r: r.net_pnl)

    def to_dict(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "rows": [r.to_dict() for r in self.rows],
            "cost_usd": round(self.cost_usd, 4),
            "skipped": self.skipped,
        }

    def summary(self) -> str:
        lines = [f"[scan] {self.spec_id} across {len(self.rows)} instrument(s):"]
        lines.append(
            f"    {'symbol':<10}{'trades':>8}{'net':>14}{'out-of-sample':>16}{'Sharpe':>9}  verdict"
        )
        for r in sorted(self.rows, key=lambda x: -x.oos_net_pnl):
            mark = "SURVIVES" if r.survived else "fails"
            lines.append(
                f"    {r.symbol:<10}{r.n_trades:>8}{r.net_pnl:>14,.0f}{r.oos_net_pnl:>16,.0f}"
                f"{r.sharpe:>9.2f}  {mark}"
            )
        if self.inverted:
            lines.append(
                f"    Edge is INVERTED on {', '.join(r.symbol for r in self.inverted[:3])} -- the same "
                "rules lose money there, consistently."
            )
        for symbol, why in self.skipped.items():
            lines.append(f"    skipped {symbol}: {why}")
        lines.append(f"    scan cost ${self.cost_usd:,.4f}")
        return "\n".join(lines)


def cross_asset_scan(
    spec: StrategySpec,
    symbols: list[str] | None = None,
    timeframe: str = "5m",
    loader=None,
    monte_carlo_paths: int = 300,
    fixtures_only: bool = True,
) -> ScanReport:
    """One validated strategy run across the instrument universe (ability 64).

    A spec whose risk is not scale-free is refused here rather than run: the
    same numbers would mean different things on each instrument, and the ranking
    would be meaningless.
    """
    from ee_agent.data.loader import load_bars
    from ee_agent.engine.truth import TruthEngine
    from ee_agent.instruments.registry import registry
    from ee_agent.spec.portability import is_portable, portability_note

    report = ScanReport(spec_id=spec.id)
    if not is_portable(spec):
        report.skipped["ALL"] = (
            "this spec's risk is not scale-free, so a cross-asset ranking would compare different "
            "strategies. Convert it first: " + (portability_note(spec) or "")
        )
        return report

    loader = loader or (
        lambda symbol: load_bars(
            symbol, timeframe, fixtures_only=fixtures_only, use_cache=False, sink=lambda _m: None
        )
    )
    symbols = symbols or registry().symbols()
    cost_before = ledger().total_usd

    for symbol in symbols:
        try:
            bars = loader(symbol)
        except Exception as exc:
            report.skipped[symbol] = f"no data ({exc})"
            continue
        if len(bars) < 500:
            report.skipped[symbol] = f"only {len(bars)} bars"
            continue
        try:
            variant = StrategySpec.from_dict(spec.to_dict())
            variant.universe.instruments = [symbol]
            analysis = TruthEngine(
                variant, monte_carlo_paths=monte_carlo_paths, synthetic_paths=0,
                n_variants_tested=len(symbols),
            ).analyze(bars, pin=False)
        except Exception as exc:
            report.skipped[symbol] = str(exc)
            continue
        oos = analysis.walk_forward.out_of_sample if analysis.walk_forward else None
        report.rows.append(
            ScanRow(
                symbol=symbol,
                net_pnl=analysis.base.metrics.net_pnl,
                oos_net_pnl=oos.net_pnl if oos else 0.0,
                n_trades=analysis.base.metrics.n_trades,
                sharpe=analysis.base.metrics.sharpe,
                max_drawdown=analysis.base.metrics.max_drawdown,
                survived=bool(analysis.adversarial and analysis.adversarial.survived),
                verdict=analysis.adversarial.verdict if analysis.adversarial else "",
            )
        )
    report.cost_usd = ledger().total_usd - cost_before
    return report
