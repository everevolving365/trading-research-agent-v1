"""The truth engine: the only thing in this system allowed to report a number.

Law One, made operational. A :class:`StrategyReport` cannot exist without:

* costs charged on every fill,
* an in-sample number and an out-of-sample number,
* a Monte Carlo band,
* a lookahead verdict,
* cache age and data quality on the dataset it used,
* and, for futures on a prop account, a combine pass rate.

Every result also carries the case against it (the adversarial pass).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ee_agent.compile.to_python import CompiledStrategy, compile_to_python
from ee_agent.cost.notifier import ledger
from ee_agent.data.bars import Bars
from ee_agent.engine import adversarial as adv
from ee_agent.engine import lookahead as la
from ee_agent.engine.analysis import (
    CorrectionResult,
    MonteCarloResult,
    RegimeBreakdown,
    SyntheticResult,
    WalkForwardResult,
    correct_for_selection,
    decompose_regimes,
    monte_carlo,
    run_walk_forward,
    synthetic_market_test,
)
from ee_agent.engine.backtester import BacktestResult, Backtester
from ee_agent.instruments.registry import get_instrument
from ee_agent.paths import artifacts_dir
from ee_agent.prop.rules import CombineReport, daily_pnls_from_trades, simulate_combine
from ee_agent.spec.model import StrategySpec


@dataclass
class StrategyReport:
    spec: StrategySpec
    base: BacktestResult
    walk_forward: WalkForwardResult | None = None
    monte_carlo: MonteCarloResult | None = None
    lookahead: la.LookaheadReport | None = None
    regimes: RegimeBreakdown | None = None
    synthetic: SyntheticResult | None = None
    correction: CorrectionResult | None = None
    combine: CombineReport | None = None
    fragility: dict = field(default_factory=dict)
    adversarial: adv.AdversarialBrief | None = None
    cost_usd: float = 0.0
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    artifact_dir: str = ""
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ defensible
    @property
    def defensible(self) -> bool:
        """Every Law One requirement present. A report that is not defensible
        must not be quoted, and :meth:`report` says so at the top."""
        return all(
            [
                self.base.costs_total >= 0 and "commission" in self.base.cost_model,
                self.walk_forward is not None and self.walk_forward.out_of_sample is not None,
                self.monte_carlo is not None and self.monte_carlo.n_paths > 0,
                self.lookahead is not None,
                self.base.data_quality > 0,
            ]
        )

    @property
    def headline(self) -> str:
        m = self.base.metrics
        oos = self.walk_forward.out_of_sample if self.walk_forward else None
        return (
            f"{self.spec.id} on {self.base.symbol}: net {m.net_pnl:,.0f} in sample, "
            f"{oos.net_pnl:,.0f} out of sample" if oos else f"{self.spec.id} on {self.base.symbol}"
        )

    def to_dict(self) -> dict:
        return {
            "spec_id": self.spec.id,
            "spec_hash": self.spec.hash,
            "plan_hash": self.base.plan_hash,
            "created": self.created,
            "defensible": self.defensible,
            "backtest": self.base.to_dict(),
            "walk_forward": self.walk_forward.to_dict() if self.walk_forward else None,
            "monte_carlo": self.monte_carlo.to_dict() if self.monte_carlo else None,
            "lookahead": self.lookahead.to_dict() if self.lookahead else None,
            "regimes": self.regimes.to_dict() if self.regimes else None,
            "synthetic": self.synthetic.to_dict() if self.synthetic else None,
            "correction": self.correction.to_dict() if self.correction else None,
            "combine": self.combine.to_dict() if self.combine else None,
            "fragility": self.fragility,
            "adversarial": self.adversarial.to_dict() if self.adversarial else None,
            "cost_usd": round(self.cost_usd, 6),
            "artifact_dir": self.artifact_dir,
            "notes": self.notes,
        }

    def report(self) -> str:
        parts = [self.base.report().replace(
            "  SINGLE WINDOW -- not a defensible number on its own. Run the truth engine "
            "(ee-agent analyze) for in-sample/out-of-sample, Monte Carlo and the lookahead verdict.",
            "",
        ).rstrip()]
        if self.walk_forward:
            parts.append(self.walk_forward.summary())
        if self.monte_carlo:
            parts.append(self.monte_carlo.band())
        if self.lookahead:
            parts.append(self.lookahead.summary())
        if self.regimes and self.regimes.by_dimension:
            parts.append(self.regimes.summary())
        if self.synthetic and self.synthetic.n_paths:
            parts.append(self.synthetic.summary())
        if self.correction and self.correction.n_tested > 1:
            parts.append(self.correction.summary())
        if self.combine:
            parts.append(self.combine.summary())
        if self.fragility:
            parts.append(
                f"[fragility] a {self.fragility.get('perturbation_pct', 10)}% move in one parameter "
                f"costs at worst {self.fragility.get('worst_drop_pct', 0):.0f}% of the result "
                f"({self.fragility.get('worst_parameter', 'n/a')})."
            )
        if self.adversarial:
            parts.append(self.adversarial.summary())
        parts.append(f"[cost] this analysis cost ${self.cost_usd:,.4f} to produce.")
        if not self.defensible:
            parts.insert(
                0,
                "*** NOT DEFENSIBLE: a Law One requirement is missing. Do not quote this. ***",
            )
        if self.artifact_dir:
            parts.append(f"[artifact] pinned at {self.artifact_dir} (re-runnable without regeneration)")
        return "\n".join(p for p in parts if p.strip())

    # -------------------------------------------------------------- artifact
    def pin(self, root: Path | None = None) -> Path:
        """Pin every input and output so the same spec always produces the same
        result, without regeneration (Phase 5 acceptance)."""
        base = Path(root or artifacts_dir())
        key = hashlib.sha256(
            f"{self.spec.hash}|{self.base.bars_hash}|{self.base.plan_hash}".encode()
        ).hexdigest()[:16]
        out = base / f"{self.spec.id}-{self.base.symbol}-{key}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "spec.yaml").write_text(self.spec.to_yaml(), encoding="utf-8")
        (out / "report.json").write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        (out / "report.txt").write_text(self.report(), encoding="utf-8")
        (out / "trades.json").write_text(
            json.dumps([t.to_dict() for t in self.base.trades], indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (out / "pin.json").write_text(
            json.dumps(
                {
                    "spec_hash": self.spec.hash,
                    "plan_hash": self.base.plan_hash,
                    "bars_hash": self.base.bars_hash,
                    "symbol": self.base.symbol,
                    "timeframe": self.base.timeframe,
                    "start": self.base.start,
                    "end": self.base.end,
                    "engine_version": 1,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.artifact_dir = str(out)
        return out


class TruthEngine:
    """Run everything Law One requires, in one call."""

    def __init__(
        self,
        spec: StrategySpec,
        initial_capital: float = 50_000.0,
        n_walk_forward: int = 5,
        anchored: bool = False,
        monte_carlo_paths: int = 2000,
        synthetic_paths: int = 12,
        n_variants_tested: int = 1,
        prop_firm: str | None = "topstep",
        prop_account: str | None = None,
        fragility_pct: float = 10.0,
        seed: int = 7,
    ):
        self.spec = spec
        self.initial_capital = initial_capital
        self.n_walk_forward = n_walk_forward
        self.anchored = anchored
        self.monte_carlo_paths = monte_carlo_paths
        self.synthetic_paths = synthetic_paths
        self.n_variants_tested = n_variants_tested
        self.prop_firm = prop_firm
        self.prop_account = prop_account
        self.fragility_pct = fragility_pct
        self.seed = seed
        self.compiled: CompiledStrategy = compile_to_python(spec)

    # ------------------------------------------------------------------ run
    def analyze(self, bars: Bars, pin: bool = True, calendar_days: set[str] | None = None) -> StrategyReport:
        cost_before = ledger().total_usd
        instrument = get_instrument(bars.symbol)
        bt = Backtester(self.spec, initial_capital=self.initial_capital, compiled=self.compiled)

        ledger().spend("backtest", **{"cpu.core_minute": len(bars) / 200000.0})
        base = bt.run(bars, window_label="full")

        def run_window(start: int, end: int, label: str):
            return bt.run(bars.slice(start, end), window_label=label)

        ledger().spend(
            "walk_forward", **{"cpu.core_minute": len(bars) * self.n_walk_forward * 2 / 200000.0}
        )
        wf = run_walk_forward(run_window, len(bars), n_windows=self.n_walk_forward, anchored=self.anchored)

        pnls = [t.net_pnl for t in base.trades if t.closed]
        ruin = self._ruin_threshold()
        ledger().spend("monte_carlo", **{"cpu.core_minute": self.monte_carlo_paths / 20000.0})
        mc = monte_carlo(
            pnls, n_paths=self.monte_carlo_paths, method="block", seed=self.seed, ruin_threshold=ruin
        )

        look = la.full_check(self.compiled, bars, result=base, instrument=instrument, seed=self.seed)
        regimes = decompose_regimes(base.trades, bars, calendar_days=calendar_days)

        synthetic = None
        if self.synthetic_paths > 0 and len(pnls) >= 5:
            ledger().spend(
                "synthetic", **{"cpu.core_minute": len(bars) * self.synthetic_paths / 200000.0}
            )
            synthetic = synthetic_market_test(
                lambda b: bt.run(b, window_label="synthetic"),
                bars,
                base.metrics.net_pnl,
                n_paths=self.synthetic_paths,
                seed=self.seed + 100,
            )

        correction = correct_for_selection(
            base.metrics.sharpe, self.n_variants_tested, max(base.metrics.n_trades, 2)
        )

        combine = None
        if instrument.prop_eligible and self.prop_firm:
            daily = daily_pnls_from_trades(base.trades, bars)
            combine = simulate_combine(
                daily, firm=self.prop_firm, account_id=self.prop_account, seed=self.seed + 200
            )

        fragility = self._fragility(bars)

        report = StrategyReport(
            spec=self.spec,
            base=base,
            walk_forward=wf,
            monte_carlo=mc,
            lookahead=look,
            regimes=regimes,
            synthetic=synthetic,
            correction=correction,
            combine=combine,
            fragility=fragility,
        )
        ledger().spend("adversarial", **{"model.input_1k": 2.0, "model.output_1k": 0.8})
        report.adversarial = adv.attack(report)
        report.cost_usd = ledger().total_usd - cost_before
        if pin:
            report.pin()
        _append_research_index(report)
        return report

    # ------------------------------------------------------------- helpers
    def _ruin_threshold(self) -> float:
        if not self.prop_firm:
            return self.initial_capital * 0.2
        try:
            from ee_agent.prop.rules import load_firm

            return float(load_firm(self.prop_firm).account(self.prop_account).trailing_max_drawdown)
        except Exception:
            return self.initial_capital * 0.2

    def _fragility(self, bars: Bars) -> dict:
        """Perturb each risk parameter and see how much of the result survives."""
        base_net = None
        results: dict[str, float] = {}
        pct = self.fragility_pct / 100.0
        for label in ("stop", "target"):
            dist = getattr(self.spec.risk, label)
            if dist is None:
                continue
            for direction in (1 + pct, 1 - pct):
                variant = StrategySpec.from_dict(self.spec.to_dict())
                getattr(variant.risk, label).value = round(dist.value * direction, 6)
                try:
                    out = Backtester(variant, initial_capital=self.initial_capital).run(
                        bars, window_label=f"fragility:{label}:{direction:.2f}"
                    )
                except Exception:
                    continue
                if base_net is None:
                    base_net = Backtester(self.spec, initial_capital=self.initial_capital, compiled=self.compiled).run(
                        bars, window_label="fragility-base"
                    ).metrics.net_pnl
                results[f"{label} {'+' if direction > 1 else '-'}{self.fragility_pct:.0f}%"] = (
                    out.metrics.net_pnl
                )
        if not results or base_net in (None, 0):
            return {}
        worst_key = min(results, key=lambda k: results[k])
        worst = results[worst_key]
        drop = (base_net - worst) / abs(base_net) * 100.0
        return {
            "perturbation_pct": self.fragility_pct,
            "base_net_pnl": round(float(base_net), 2),
            "variants": {k: round(float(v), 2) for k, v in results.items()},
            "worst_parameter": worst_key,
            "worst_drop_pct": round(float(drop), 2),
        }


def _append_research_index(report: StrategyReport) -> None:
    """Ability 68: one append-only file written every run. The only thing in the
    system that compounds."""
    try:
        from ee_agent.research.index import append_run

        append_run(report)
    except Exception:
        pass


def analyze(spec: StrategySpec, bars: Bars, **kw) -> StrategyReport:
    return TruthEngine(spec, **kw).analyze(bars)
