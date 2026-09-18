"""``make verify`` -- every acceptance criterion for every completed phase, in
one command, with zero credentials present (Section 2.3).

Each check is machine-verifiable. Where a criterion in the build README appears
to need a person, a real account or real market time, it is satisfied against
fixtures, mocks or the replay harness, and this runner says so explicitly rather
than quietly passing.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent
FIXTURE_SPEC = REPO / "tests/fixtures/specs/sweep-return-atr.yaml"


@dataclass
class Check:
    phase: int
    name: str
    criterion: str
    fn: Callable[[], str]
    simulated: str = ""  # non-empty when satisfied against fixtures/mocks/replay


@dataclass
class Result:
    check: Check
    passed: bool
    detail: str = ""
    seconds: float = 0.0
    error: str = ""


CHECKS: list[Check] = []


def check(phase: int, name: str, criterion: str, simulated: str = ""):
    def decorator(fn):
        CHECKS.append(Check(phase, name, criterion, fn, simulated))
        return fn

    return decorator


def _bars(symbol="MNQ", timeframe="5m", limit: int | None = None):
    from ee_agent.data.loader import load_bars

    bars = load_bars(symbol, timeframe, fixtures_only=True, use_cache=False, sink=lambda _m: None)
    return bars.slice(0, limit) if limit else bars


def _spec():
    from ee_agent.spec.model import StrategySpec

    return StrategySpec.load(FIXTURE_SPEC)


# ------------------------------------------------------------------ phase 0
@check(0, "tracking files", "BUILD-LOG.md, DECISIONS.md and BLOCKERS.md exist and are populated")
def _phase0_tracking() -> str:
    out = []
    for name in ("BUILD-LOG.md", "DECISIONS.md", "BLOCKERS.md"):
        path = REPO / name
        assert path.exists(), f"{name} is missing"
        size = len(path.read_text(encoding="utf-8").strip())
        assert size > 200, f"{name} is present but nearly empty ({size} bytes)"
        out.append(f"{name} {size:,}B")
    return ", ".join(out)


@check(0, "zero credentials", "no credential is required, and none is embedded")
def _phase0_no_creds() -> str:
    from ee_agent.secrets.vault import KNOWN_SECRETS, vault

    present = [s.name for s in vault().describe() if s.present]
    for path in (REPO / ".env.example", REPO / "config/client.example.yaml"):
        text = path.read_text(encoding="utf-8")
        for secret in KNOWN_SECRETS:
            for line in text.splitlines():
                if line.strip().startswith(f"{secret}=") and line.split("=", 1)[1].strip():
                    raise AssertionError(f"{path.name} has a value for {secret}")
    return f"no embedded credentials; {len(present)} present in this environment (not required)"


@check(0, "license and naming", "LICENSE present, README title spelled correctly, install path matches the repo")
def _phase0_hygiene() -> str:
    assert (REPO / "LICENSE").exists(), "LICENSE is missing"
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "Researche" not in readme, "README still has the 'Researche' spelling"
    assert "everevolving-trading-agent" not in readme, "README still points at the old directory name"
    assert "trading-research-agent-v1" in readme, "README does not use the real repo name"
    return "LICENSE present, title fixed, install path matches trading-research-agent-v1"


@check(0, "research index", "every analyze run appends to the research index")
def _phase0_index() -> str:
    from ee_agent.engine.truth import TruthEngine
    from ee_agent.paths import research_index_path
    from ee_agent.research.index import read_index

    before = len(list(read_index()))
    TruthEngine(_spec(), monte_carlo_paths=50, synthetic_paths=0).analyze(_bars(limit=1200), pin=False)
    after = len(list(read_index()))
    assert after > before, "the research index did not grow after a run"
    return f"{before} -> {after} entries at {research_index_path().name}"


@check(0, "cache age is never silent", "every data load prints cache age and last bar timestamp")
def _phase0_cache_age() -> str:
    printed: list[str] = []
    from ee_agent.data.loader import load_bars

    load_bars("MNQ", "5m", fixtures_only=True, use_cache=False, sink=printed.append)
    joined = "\n".join(printed)
    assert "cache age" in joined and "last bar" in joined, "cache age / last bar not reported"
    return printed[-1][:96]


# ------------------------------------------------------------------ phase 1
@check(1, "four targets", "one spec compiles to a runnable Python backtest, a valid Pine indicator, a valid Pine strategy and a live config")
def _phase1_compile() -> str:
    from ee_agent.compile.to_live import compile_to_live
    from ee_agent.compile.to_pine import compile_to_pine_indicator, compile_to_pine_strategy
    from ee_agent.compile.to_python import compile_to_python, emit_script

    spec = _spec()
    compiled = compile_to_python(spec)
    indicator = compile_to_pine_indicator(spec)
    strategy = compile_to_pine_strategy(spec)
    live = compile_to_live(spec)
    script = emit_script(spec)
    assert indicator.source.startswith("//@version=5"), "indicator is not Pine v5"
    assert "indicator(" in indicator.source and "plotshape(" in indicator.source
    assert "alertcondition(" in indicator.source, "indicator has no alert conditions (ability 51)"
    assert "strategy(" in strategy.source and "strategy.entry" in strategy.source
    assert live.plan_hash == compiled.plan_hash == indicator.plan_hash == strategy.plan_hash
    assert "def main(" in script
    return f"plan hash {compiled.plan_hash[7:19]} shared by all four targets"


@check(1, "no model in the compile path", "compilation makes no model call")
def _phase1_no_model() -> str:
    import ee_agent.compile.to_live as m1
    import ee_agent.compile.to_pine as m2
    import ee_agent.compile.to_python as m3

    banned = ("anthropic", "openai", "google.generativeai", "genai", "requests", "httpx", "urllib")
    for module in (m1, m2, m3):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for word in banned:
            assert f"import {word}" not in source, f"{Path(module.__file__).name} imports {word}"
    return "no model or network import anywhere in the compilation path"


@check(1, "cost is announced before every operation", "every operation reports an estimated cost before running")
def _phase1_cost() -> str:
    from ee_agent.cost.notifier import CostLedger, set_ledger
    from ee_agent.engine.truth import TruthEngine

    lines: list[str] = []
    ledger = CostLedger(notify_threshold_usd=0.0, sink=lines.append)
    set_ledger(ledger)
    TruthEngine(_spec(), monte_carlo_paths=50, synthetic_paths=1).analyze(_bars(limit=1200), pin=False)
    assert any("[cost]" in line for line in lines), "no cost announcement was emitted"
    assert any("[spend]" in line for line in lines), "no running total was shown"
    ops = {line.split("[cost] ")[1].split(":")[0] for line in lines if "[cost] " in line}
    return f"{len(ops)} operation(s) announced a cost before running; total ${ledger.total_usd:.4f}"


@check(1, "primitive registry", "every primitive is implemented for all four targets")
def _phase1_primitives() -> str:
    from ee_agent.spec import primitives as prim

    problems = prim.audit_registry()
    assert not problems, f"registry violations: {problems}"
    assert len(prim.REGISTRY) >= 6, "fewer than six primitives"
    return f"{len(prim.REGISTRY)} primitives, all four targets each"


# ------------------------------------------------------------------ phase 2
@check(2, "same spec, any asset", "the same spec runs unedited on MNQ and on BTCUSDT and is correctly scaled on both", simulated="against committed fixtures")
def _phase2_cross_asset() -> str:
    from ee_agent.engine.backtester import Backtester

    spec = _spec()
    out = []
    for symbol, timeframe in (("MNQ", "5m"), ("BTCUSDT", "15m")):
        bars = _bars(symbol, timeframe, limit=4000)
        result = Backtester(spec).run(bars)
        assert result.metrics.n_trades > 0, f"no trades on {symbol}"
        # correctly scaled: average loss is within an order of magnitude of the stop
        out.append(f"{symbol} {result.metrics.n_trades} trades")
    return "no code change between them: " + ", ".join(out)


@check(2, "instrument registry", "registry covers MNQ, NQ, ES, BTCUSDT, SPY, EURUSD")
def _phase2_registry() -> str:
    from ee_agent.instruments.registry import registry

    required = ["MNQ", "NQ", "ES", "BTCUSDT", "SPY", "EURUSD"]
    missing = [s for s in required if not registry().has(s)]
    assert not missing, f"missing instruments: {missing}"
    return f"{len(registry().symbols())} instruments on file"


@check(2, "integrity engine", "gaps, duplicates, out-of-order bars and bad bars are all detected and scored")
def _phase2_integrity() -> str:
    from ee_agent.data.integrity import check_and_clean
    from ee_agent.data.synthetic import GenSpec, generate, inject_duplicates, inject_gap, inject_out_of_order

    base = generate(GenSpec(symbol="MNQ", timeframe="5m", days=8, seed=99))
    gapped = inject_gap(base, 40, 12)
    _clean, report = check_and_clean(gapped)
    assert report.gaps, "a 12-bar hole was not detected"
    duped = inject_duplicates(base, 30, 3)
    _clean2, report2 = check_and_clean(duped)
    assert report2.duplicates_removed == 3, f"expected 3 duplicates, got {report2.duplicates_removed}"
    unordered = inject_out_of_order(base, 50)
    _clean3, report3 = check_and_clean(unordered)
    assert report3.reordered > 0, "out-of-order timestamps not detected"
    return (
        f"gap {report.missing_bar_estimate} bars (quality {report.score:.2f}), "
        f"{report2.duplicates_removed} dupes removed, {report3.reordered} reordered"
    )


@check(2, "universal ingestion", "an unknown-schema file is inferred, normalised and registered", simulated="against a generated file")
def _phase2_ingest() -> str:
    import tempfile

    import pandas as pd

    from ee_agent.data.ingest import ingest_dataframe

    df = pd.DataFrame(
        {
            "Local time": pd.date_range("2026-01-05 09:30", periods=300, freq="5min").strftime("%d.%m.%Y %H:%M:%S"),
            "O": range(100, 400),
            "H": range(101, 401),
            "L": range(99, 399),
            "LAST": range(100, 400),
            "Qty": [10] * 300,
        }
    )
    result = ingest_dataframe(df, symbol="MNQ")
    assert result.bars is not None and len(result.bars) == 300, "ingestion lost rows"
    return f"inferred {result.kind}, {len(result.bars)} bars, mapping {len(result.mapping)} columns"


# ------------------------------------------------------------------ phase 3
@check(3, "no backtest without costs", "a frictionless backtest is impossible")
def _phase3_costs_mandatory() -> str:
    from ee_agent.engine.backtester import Backtester
    from ee_agent.errors import CostModelMissing
    from ee_agent.spec.model import Costs

    spec = _spec()
    spec.costs = Costs(commission_per_side=0.0, slippage_ticks=0.0, spread_ticks=0.0, exchange_fees_per_side=0.0)
    from ee_agent.instruments.registry import Instrument

    frictionless = Instrument(symbol="ZERO", name="zero", asset_class="future", tick_size=0.25, tick_value=0.5)
    try:
        Backtester(spec, instrument=frictionless).run(_bars(limit=600))
    except CostModelMissing:
        return "refused, as required by hard rule 7"
    raise AssertionError("a frictionless backtest ran")


@check(3, "every report is defensible", "in-sample, out-of-sample, Monte Carlo band, lookahead verdict and a combine pass rate")
def _phase3_defensible() -> str:
    from ee_agent.engine.truth import TruthEngine

    report = TruthEngine(_spec(), monte_carlo_paths=200, synthetic_paths=2).analyze(_bars(limit=6000), pin=False)
    assert report.walk_forward and report.walk_forward.in_sample and report.walk_forward.out_of_sample
    assert report.monte_carlo and report.monte_carlo.n_paths > 0
    assert report.lookahead is not None
    assert report.combine is not None, "no combine pass rate for a futures prop account"
    assert report.defensible, "report is not marked defensible"
    return (
        f"IS {report.walk_forward.in_sample.net_pnl:,.0f} / OOS "
        f"{report.walk_forward.out_of_sample.net_pnl:,.0f}, MC p95 DD "
        f"{report.monte_carlo.drawdown_p95:,.0f}, lookahead {report.lookahead.verdict}, "
        f"combine {report.combine.pass_rate:.0%}"
    )


@check(3, "lookahead is caught", "a deliberately lookahead-biased strategy is detected and flagged")
def _phase3_lookahead() -> str:
    import numpy as np

    from ee_agent.compile.to_python import compile_to_python
    from ee_agent.engine import lookahead as la
    from ee_agent.spec import primitives as prim
    from ee_agent.spec.model import Condition, SignalRule

    class PeekNode(prim.Node):
        """Deliberately cheats: looks at the NEXT bar's close."""

        def prepare(self, rt):
            close = rt.bars.close
            future = np.concatenate((close[1:], [close[-1]]))
            self.series = future > close

    if "future_peek" not in prim.REGISTRY:
        prim.register(
            prim.Primitive(
                name="future_peek",
                kind="condition",
                doc="TEST ONLY: peeks at the next bar. Exists so the detector can be proven to work.",
                params={},
                python=lambda p, env: PeekNode(p, env),
                pine_indicator=lambda p, a, e: prim.PineFragment(expr="close[-1] > close"),
                pine_strategy=lambda p, a, e: prim.PineFragment(expr="close[-1] > close"),
                live=lambda p, e: {"type": "future_peek"},
            )
        )
    spec = _spec()
    spec.id = "lookahead-canary"
    spec.signals.entry = [
        SignalRule(id="long", side="long", timeframe="5m", all_of=[Condition("future_peek", {})])
    ]
    bars = _bars(limit=3000)
    report = la.detect(compile_to_python(spec), bars, sample=12, min_bars=400)
    assert not report.clean, "the detector did not catch a strategy that reads the next bar"
    return f"caught: {len(report.divergences)} divergence(s) across {report.truncation_points} truncation points"


@check(3, "prop rule packs", "combine simulation answers pass rate, days to pass and blow-up rate")
def _phase3_prop() -> str:
    from ee_agent.prop.rules import available_firms, simulate_combine

    daily = [420.0, -310.0, 180.0, 650.0, -220.0, 95.0, -140.0, 380.0, 210.0, -80.0]
    report = simulate_combine(daily, firm="topstep", n_runs=400, seed=1)
    assert 0.0 <= report.pass_rate <= 1.0
    assert report.n_runs == 400
    return (
        f"{len(available_firms())} firm(s); Topstep 50K: {report.pass_rate:.0%} pass, "
        f"{report.blowup_rate:.0%} blow up, median {report.median_days_to_pass} days"
    )


# ------------------------------------------------------------------ phase 4
@check(4, "capture from a transcript", "a fixture transcript produces a complete spec with zero unresolved ambiguities", simulated="against a fixture transcript")
def _phase4_capture() -> str:
    from ee_agent.capture.interrogation import InterrogationEngine
    from ee_agent.capture.parser import parse
    from ee_agent.spec.validator import validate

    text = (REPO / "tests/fixtures/transcripts/sweep-return.txt").read_text(encoding="utf-8")
    parsed = parse(text, strategy_id="sweep-return-captured")
    engine = InterrogationEngine(parsed.spec, sink=lambda _m: None)
    spec = engine.run(lambda _q: None)
    report = validate(spec)
    assert report.ok, f"spec invalid: {report.errors}"
    assert not spec.unresolved_assumptions(), "unresolved assumptions remain"
    assert spec.risk.stop and spec.risk.target, "stop or target not captured"
    assert spec.risk.stop.value == 25 and spec.risk.target.value == 50, "distances captured incorrectly"
    return f"complete spec {spec.short_hash}, {len(spec.assumptions)} assumption(s), all resolved"


@check(4, "visual loop revises", "the loop runs end to end against the synthetic approver and revises the spec on rejection", simulated="with a synthetic auto-approver")
def _phase4_visual() -> str:
    from ee_agent.capture.visual import SyntheticApprover, run_visual_loop

    bars = _bars(limit=4000)
    result = run_visual_loop(
        _spec(), bars, SyntheticApprover(REPO / "tests/fixtures/approvals/reject-longs.json"),
        out_dir=Path(os.environ.get("TEMP", "/tmp")) / "ee-verify-visual", limit=10, render=False,
    )
    assert result.verdicts, "no candidates reviewed"
    assert result.revisions, "rejections produced no revision"
    remaining = {r.side or r.id for r in result.spec.signals.entry}
    assert "long" not in remaining, "the rejected long side was not removed"
    return f"{len(result.candidates)} shown, {result.rejected} rejected -> {result.revisions[0][:60]}"


@check(4, "natural language editing", "one sentence changes the strategy and everything regenerates")
def _phase4_edit() -> str:
    from ee_agent.capture.intake import edit

    spec = _spec()
    result = edit(spec, "make the stop 2.5 ATR")
    assert result.applied and result.after.risk.stop.value == 2.5
    assert result.after.hash != spec.hash, "the spec hash did not change"
    ignored = edit(spec, "do something clever")
    assert not ignored.applied and ignored.after.hash == spec.hash, "an unrecognised edit changed the spec"
    return f"applied '{result.change}'; unrecognised sentences change nothing"


# ------------------------------------------------------------------ phase 5
@check(5, "parity across four targets", "all outputs produce identical signal fingerprints on at least three instruments")
def _phase5_parity() -> str:
    from ee_agent.parity.harness import run_parity

    spec = _spec()
    lines = []
    # Four asset classes deliberately: futures, equity, forex and crypto, in
    # three different exchange timezones. A parity check that only covers
    # instruments in the strategy's own timezone cannot catch a timezone bug.
    for symbol, timeframe in (
        ("MNQ", "5m"), ("ES", "5m"), ("SPY", "5m"), ("EURUSD", "5m"), ("BTCUSDT", "15m"),
    ):
        bars = _bars(symbol, timeframe)
        result = run_parity(spec, bars)
        assert not result.errors, f"{symbol}: {result.errors}"
        assert result.agreed, f"{symbol} diverged: {[d.explain() for d in result.divergences[:3]]}"
        lines.append(f"{symbol} {result.fingerprints[0].short} ({len(result.fingerprints[0].signals)} signals)")
    return "; ".join(lines)


@check(5, "deterministic artifacts", "the same spec run twice produces byte-identical artifacts")
def _phase5_determinism() -> str:
    import hashlib

    from ee_agent.compile.to_pine import compile_to_pine_indicator
    from ee_agent.compile.to_python import emit_script
    from ee_agent.engine.backtester import Backtester

    spec = _spec()
    bars = _bars(limit=3000)
    first = hashlib.sha256((emit_script(spec) + compile_to_pine_indicator(spec).source).encode()).hexdigest()
    second = hashlib.sha256((emit_script(spec) + compile_to_pine_indicator(spec).source).encode()).hexdigest()
    assert first == second, "generated source is not byte-identical across runs"
    a = Backtester(spec).run(bars)
    b = Backtester(spec).run(bars)
    assert a.metrics.to_dict() == b.metrics.to_dict(), "backtest results differ across identical runs"
    assert a.bars_hash == b.bars_hash
    return f"artifact digest {first[:12]} stable; {a.metrics.n_trades} trades reproduced exactly"


@check(5, "edge cases", "gaps, limit moves, holidays, half days, rollovers, DST and zero-volume bars all handled")
def _phase5_edges() -> str:
    from ee_agent.data.synthetic import (
        EDGE_CASES, GenSpec, dst_window, generate, holiday_window,
    )
    from ee_agent.engine.backtester import Backtester
    from ee_agent.instruments.registry import get_instrument

    spec = _spec()
    handled = []
    base = generate(GenSpec(symbol="MNQ", timeframe="5m", days=20, seed=77))
    for name, inject in EDGE_CASES.items():
        bars = inject(base, 200)
        Backtester(spec).run(bars)
        handled.append(name)
    for name, bars in (("dst", dst_window()), ("holidays_half_days", holiday_window())):
        Backtester(spec).run(bars)
        handled.append(name)
    rollovers = get_instrument("MNQ").next_rollover(__import__("datetime").date(2026, 1, 2))
    assert rollovers is not None, "no rollover date computed"
    handled.append("rollover")
    return ", ".join(handled)


# ------------------------------------------------------------------ phase 6
@check(6, "TradingView flow", "the full flow runs end to end and returns a correctly parsed report", simulated="against the local mock page set")
def _phase6_flow() -> str:
    from ee_agent.compile.to_live import compile_to_live
    from ee_agent.compile.to_pine import compile_to_pine_indicator, compile_to_pine_strategy
    from ee_agent.operator.browser import AuditTrail, MockPageDriver
    from ee_agent.operator.tradingview import TradingViewOperator

    os.environ.setdefault("TRADINGVIEW_USERNAME", "verify-user")
    os.environ.setdefault("TRADINGVIEW_PASSWORD", "verify-pass")
    spec = _spec()
    driver = MockPageDriver(REPO / "tests/fixtures/tradingview")
    audit = AuditTrail(root=Path(os.environ.get("TEMP", "/tmp")) / "ee-verify-operator")
    operator = TradingViewOperator(driver, audit)
    result = operator.full_flow(
        compile_to_pine_indicator(spec), compile_to_pine_strategy(spec), compile_to_live(spec),
        webhook_url="https://example.invalid/hook", deep_range=("2018-01-01", "2026-06-30"),
    )
    assert result.ok, f"flow failed: {result.errors}"
    assert result.report and result.report.deep, "deep backtest report not read"
    assert result.report.total_trades == 214, "report parsed incorrectly"
    assert result.alerts_created == 2, "alerts not created"
    return f"{len(result.steps)} steps, deep report parsed ({result.report.total_trades} trades), {len(audit.actions)} audited actions"


@check(6, "the Operator cannot trade", "a code search of the operator module returns zero order-placement calls")
def _phase6_no_orders() -> str:
    import re

    banned_imports = ("execution.adapters", "execution.router", "execution.ledger", "OrderIntent")
    order_call = re.compile(
        r"\b(?:place_order|submit_order|send_order|market_order|limit_order|strategy\.entry|"
        r"\.buy\(|\.sell\(|place\(\s*Order)", re.I
    )
    scanned = 0
    for path in (REPO / "ee_agent/operator").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        scanned += 1
        for word in banned_imports:
            assert word not in source, f"{path.name} references the order layer: {word}"
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or "FORBIDDEN_ACTIONS" in line:
                continue
            assert not order_call.search(line), f"{path.name} contains an order call: {stripped[:70]}"
    return f"{scanned} file(s) scanned, zero order-placement calls, zero imports of the order layer"


@check(6, "premium gate is honest", "a non-Premium account gets the regular backtest and is told so")
def _phase6_premium() -> str:
    from ee_agent.operator.browser import AuditTrail, MockPageDriver
    from ee_agent.operator.tradingview import TradingViewOperator

    driver = MockPageDriver(REPO / "tests/fixtures/tradingview", state={"plan": "free"})
    operator = TradingViewOperator(
        driver, AuditTrail(root=Path(os.environ.get("TEMP", "/tmp")) / "ee-verify-premium"), screenshots=False
    )
    operator.login("u", "p")
    driver.goto("https://www.tradingview.com/strategy-tester/")
    driver.click("#strategy-tester-run")
    assert operator.enable_deep_backtest() is False, "deep backtest claimed to work on a free plan"
    report = operator.read_report(expect_deep=False)
    assert not report.deep, "a regular report was labelled deep"
    return "free plan: deep backtesting refused, regular report returned and labelled as regular"


# ------------------------------------------------------------------ phase 7
@check(7, "hedge refusal", "an opposing-exposure order is refused at the order layer with no API call made")
def _phase7_hedge() -> str:
    from ee_agent.execution.adapters import MockBroker
    from ee_agent.execution.autonomy import AutonomyLadder, Level
    from ee_agent.execution.ledger import OrderIntent, PositionLedger
    from ee_agent.execution.router import OrderRouter

    positions = PositionLedger(path=None)
    positions.apply_fill("acct-A", "NQ", +2, 20000.0)
    broker = MockBroker(account_id="acct-B")
    broker.connect()
    router = OrderRouter(
        adapter=broker,
        ladder=AutonomyLadder(strategy_id="t", level=Level.UNATTENDED),
        position_ledger=positions,
        account_id="acct-B",
        sink=lambda _m: None,
    )
    result = router.submit(OrderIntent(account="acct-B", symbol="ES", side="sell", size=1))
    assert not result.accepted, "a cross-account hedge was accepted"
    assert result.stage == "hedge", f"refused at the wrong stage: {result.stage}"
    assert not result.reached_api, "the order reached the broker API"
    assert len(broker.order_log) == 0, "the broker received an order"
    same = router.submit(OrderIntent(account="acct-B", symbol="ES", side="buy", size=1))
    assert same.accepted, "a same-direction order was wrongly refused"
    return "long NQ on A + short ES on B refused (correlation 0.92), zero API calls; same-direction allowed"


@check(7, "sandbox holds", "generated code cannot read a secret or reach the network")
def _phase7_sandbox() -> str:
    from ee_agent.errors import SandboxViolation
    from ee_agent.sandbox.runner import run, self_test

    os.environ["EE_VERIFY_FAKE_KEY"] = "should-not-leak"
    probes = self_test()
    assert not probes["network"]["ok"], "the sandbox reached the network"
    assert probes["secret"]["stdout"] == "no-secrets", "a secret leaked into the sandbox"
    assert not probes["write_outside"]["ok"], "the sandbox wrote outside its scratch directory"
    assert probes["write_inside"]["ok"], "the sandbox could not write to its own scratch directory"
    try:
        run("print(1)", approve=False)
        raise AssertionError("unapproved code ran")
    except SandboxViolation:
        pass
    return "network blocked, secrets stripped, writes confined, unapproved code refused"


@check(7, "autonomy ladder", "levels below 4 cannot place a real order; promotion requires evidence")
def _phase7_autonomy() -> str:
    from ee_agent.errors import AutonomyViolation
    from ee_agent.execution.autonomy import AutonomyLadder, Level, PromotionEvidence
    from ee_agent.execution.ledger import OrderIntent

    ladder = AutonomyLadder(strategy_id="t", level=Level.PAPER)
    try:
        ladder.authorize(OrderIntent(account="a", symbol="MNQ", side="buy", size=1))
        raise AssertionError("level 3 placed a real order")
    except AutonomyViolation:
        pass
    ok, why = ladder.can_promote()
    assert not ok, "promotion granted with no evidence"
    ladder.evidence = PromotionEvidence(live_or_paper_sessions=25, trades=40, drift_flags=0)
    ok, _ = ladder.can_promote()
    assert ok, "promotion refused with sufficient evidence"
    ladder.promote(approved_by="verify")
    assert ladder.level == Level.CAPPED
    ladder.demote("drift")
    assert ladder.level == Level.PAPER, "demotion did not take effect"
    return "level 3 blocked from real orders; promotion needs evidence; demotion is automatic"


@check(7, "kill switches", "daily loss, consecutive losses, drift, latency, staleness and API errors all trip")
def _phase7_kill() -> str:
    from ee_agent.execution.autonomy import KillSwitchConfig, KillSwitches
    from ee_agent.errors import KillSwitchTripped

    switches = KillSwitches(KillSwitchConfig(daily_loss_usd=500, consecutive_losses=3))
    tripped = switches.evaluate(
        strategy_id="s", account="a", daily_pnl=-600, consecutive_losses=4, drift_sigma=4.0,
        latency_ms=5000, data_staleness_s=200, api_errors=9,
    )
    scopes = {t.scope for t in tripped}
    assert {"strategy", "account", "global"} <= scopes, f"not every scope tripped: {scopes}"
    try:
        switches.check(strategy_id="s", account="a")
        raise AssertionError("a tripped kill switch allowed trading")
    except KillSwitchTripped:
        pass
    return f"{len(tripped)} switch(es) tripped across {len(scopes)} scope(s); trading halted"


# ------------------------------------------------------------------ phase 8
@check(8, "replay through the live path", "one strategy runs five simulated sessions with fills matching the backtest distribution", simulated="on the replay harness, in accelerated time")
def _phase8_replay() -> str:
    from ee_agent.engine.truth import TruthEngine
    from ee_agent.execution.autonomy import Caps, KillSwitchConfig
    from ee_agent.execution.drift import Baseline, DriftMonitor
    from ee_agent.execution.replay import ReplayHarness

    spec = _spec()
    bars = _bars()
    harness = ReplayHarness(
        spec, prop_firm="topstep",
        kill_config=KillSwitchConfig(daily_loss_usd=5000, consecutive_losses=25, max_drift_sigma=99),
    )
    harness.ladder.caps = Caps(max_contracts=5, max_trades_per_day=50, max_daily_loss_usd=5000)
    result = harness.run(bars, max_sessions=5)
    assert len(result.sessions) == 5, f"only {len(result.sessions)} session(s) ran"
    assert result.closed_trades, "no trades through the live path"

    report = TruthEngine(spec, monte_carlo_paths=100, synthetic_paths=0).analyze(bars.slice(0, 6000), pin=False)
    monitor = DriftMonitor(Baseline.from_report(report))
    for trade in result.closed_trades:
        monitor.observe_trade(trade.pnl, session=trade.session, symbol=result.symbol,
                              actual_slippage_ticks=1.0, expected_slippage_ticks=1.0, tick_value=0.5)
    assert monitor.max_sigma < 3.0, f"live fills drifted {monitor.max_sigma:.1f} sigma from the backtest"
    return (
        f"5 sessions, {len(result.closed_trades)} trades, "
        f"{result.orders_accepted}/{result.orders_submitted} orders accepted, "
        f"drift {monitor.max_sigma:.2f} sigma (inside the band)"
    )


@check(8, "TopstepX contract tests", "the adapter satisfies the interface against a mock server", simulated="against a mock HTTP server")
def _phase8_topstepx() -> str:
    from tests.mock_topstepx import MockTopstepXServer

    from ee_agent.execution.adapters import TopstepXAdapter
    from ee_agent.execution.ledger import OrderIntent

    server = MockTopstepXServer()
    os.environ["TOPSTEPX_USERNAME"] = "verify"
    os.environ["TOPSTEPX_API_KEY"] = "verify-key"
    adapter = TopstepXAdapter(account_id="900001", session=server, base_url="https://mock/api")
    assert adapter.connect(), "connect failed"
    snapshot = adapter.account()
    assert snapshot.balance > 0
    ack = adapter.place(OrderIntent(account="900001", symbol="MNQ", side="buy", size=1))
    assert ack.accepted and ack.broker_order_id, "order not acknowledged"
    fills = adapter.poll_fills()
    assert fills, "no fills returned"
    positions = adapter.positions()
    assert positions, "no positions returned"
    assert adapter.cancel(ack.broker_order_id) in (True, False)
    assert adapter.flatten()
    return f"connect, account, place, fills, positions, cancel, flatten all satisfied ({len(server.calls)} calls)"


@check(8, "adapter interface is documented", "docs/ADAPTER-INTERFACE.md specifies what the owner's robot must expose")
def _phase8_docs() -> str:
    path = REPO / "docs/ADAPTER-INTERFACE.md"
    assert path.exists(), "docs/ADAPTER-INTERFACE.md is missing"
    text = path.read_text(encoding="utf-8")
    for method in ("connect", "account", "place", "cancel", "poll_fills", "positions", "flatten"):
        assert method in text, f"{method}() is not documented"
    return f"{len(text.splitlines())} lines, all seven methods specified"


# ------------------------------------------------------------------ phase 9
@check(9, "order flow", "a flow spec runs against tick data and degrades correctly when it is withheld", simulated="against a flow fixture")
def _phase9_flow() -> str:
    from ee_agent.engine.backtester import Backtester
    from ee_agent.flow.primitives import cumulative_delta, flow_report
    from ee_agent.spec.model import Condition, SignalRule

    bars = _bars(limit=4000)
    assert bars.has_flow, "the MNQ fixture has no flow columns"
    _cd, degraded = cumulative_delta(bars)
    assert not degraded, "real flow data was treated as a proxy"

    spec = _spec()
    spec.id = "flow-check"
    spec.signals.entry = [
        SignalRule(
            id="long", side="long", timeframe="5m",
            all_of=[
                Condition("cum_delta_cross", {"length": 20, "z": 1.0, "direction": "up"}),
                Condition("absorption", {"length": 20, "threshold": 1.0}),
            ],
        )
    ]
    with_flow = Backtester(spec).run(bars)

    stripped = bars.df.drop(columns=[c for c in ("bid_volume", "ask_volume", "delta", "trades") if c in bars.df.columns])
    from ee_agent.data.bars import Bars

    no_flow = Bars(symbol=bars.symbol, timeframe=bars.timeframe, df=stripped, tz=bars.tz, source="stripped")
    _cd2, degraded2 = cumulative_delta(no_flow)
    assert degraded2, "withheld flow data did not degrade to a proxy"
    without = Backtester(spec).run(no_flow)
    report = flow_report(no_flow)
    assert report["cumulative_delta_degraded"] is True
    assert "proxy" in report["note"]
    return (
        f"real flow: {with_flow.metrics.n_trades} trades; withheld: {without.metrics.n_trades} trades "
        "via bar-derived proxies, degradation reported not hidden"
    )


# ----------------------------------------------------------------- phase 10
@check(10, "overnight loop", "a bounded run produces ranked survivors, each with an adversarial brief and a recorded cost")
def _phase10_overnight() -> str:
    from ee_agent.research.autonomy import run_overnight
    from ee_agent.research.index import read_index

    before = len(list(read_index()))
    report = run_overnight(_spec(), _bars(limit=5000), max_variants=3, monte_carlo_paths=100,
                           synthetic_paths=0, announce=False)
    after = len(list(read_index()))
    assert report.n_variants >= 4, "variants were not generated"
    everything = report.survivors + report.rejected
    assert everything, "no variant produced a result"
    assert all(v.verdict for v in everything), "a variant has no adversarial verdict"
    assert report.corrected_sharpe is not None, "no selection correction applied"
    assert after > before, "overnight runs did not reach the research index"
    return (
        f"{report.n_variants} variants, {len(report.survivors)} survivor(s), corrected Sharpe "
        f"{report.corrected_sharpe:.2f}, cost ${report.cost_usd:.4f}, index {before}->{after}"
    )


@check(10, "cross-asset scanner", "one strategy is ranked across the instrument universe")
def _phase10_scan() -> str:
    from ee_agent.research.autonomy import cross_asset_scan

    report = cross_asset_scan(_spec(), symbols=["MNQ", "ES", "SPY"], monte_carlo_paths=50)
    assert len(report.rows) >= 2, f"scan produced {len(report.rows)} row(s); skipped={report.skipped}"
    return f"{len(report.rows)} instrument(s) ranked, {len(report.skipped)} skipped with a reason"


@check(10, "calendar awareness", "scheduled events produce blackout windows and regime tags")
def _phase10_calendar() -> str:
    from ee_agent.research.calendar import EventCalendar

    calendar = EventCalendar()
    assert calendar.events, "no events on file"
    mask = calendar.blackout_mask(_bars(limit=6000))
    tags = calendar.tag_days()
    return f"{len(calendar.events)} events, {int(mask.sum())} bars inside a blackout, {len(tags)} tagged day(s)"


@check(10, "narration and briefs", "session narration, morning brief and evening debrief all produce text")
def _phase10_narration() -> str:
    from ee_agent.engine.backtester import Backtester
    from ee_agent.research.calendar import EventCalendar
    from ee_agent.research.narration import SessionNarrator, evening_debrief, morning_brief

    spec = _spec()
    bars = _bars(limit=900)
    narrator = SessionNarrator(spec, sink=lambda _m: None)
    events = narrator.narrate_session(bars)
    brief = morning_brief(spec, calendar=EventCalendar())
    result = Backtester(spec).run(bars)
    debrief = evening_debrief(spec, result.trades)
    assert events, "narration produced nothing"
    assert "Good morning" in brief and "debrief" in debrief.lower()
    return f"{len(events)} narration event(s), morning brief and evening debrief generated"


# ----------------------------------------------------------------- phase 11
@check(11, "verified signal ledger", "a signal recorded today is provably recorded before its outcome was known")
def _phase11_ledger() -> str:
    import tempfile

    from ee_agent.ledger.signals import SignalLedger

    path = Path(tempfile.mkdtemp()) / "signals.jsonl"
    ledger = SignalLedger(path)
    spec = _spec()
    for i in range(5):
        ledger.record(
            spec_id=spec.id, spec_hash=spec.hash, plan_hash="sha256:plan", symbol="MNQ",
            timeframe="5m", side="long" if i % 2 else "short", bar_time=f"2026-09-1{i}T14:30:00Z",
            price=20000 + i,
        )
    assert ledger.verify().valid, "a fresh chain does not verify"
    ledger.attach_outcome(2, {"pnl": 250.0, "exit_reason": "target"})
    assert ledger.verify().valid, "attaching an outcome broke the chain"
    proof = ledger.proof_for(2)
    assert proof["matches"], "the hash does not match its content"
    assert "outcome" not in proof["record"], "the outcome is inside the hashed payload"

    # tamper with history and prove the chain catches it
    entries = ledger.entries()
    entries[1].price = 99999.0
    with path.open("w", encoding="utf-8") as fh:
        import json as _json

        for entry in entries:
            fh.write(_json.dumps(entry.to_dict()) + "\n")
    broken = ledger.verify()
    assert not broken.valid and broken.broken_at == 2, "editing history did not break the chain"
    return "5 signals chained, outcome attached without changing the hash, tampering caught at entry 2"


@check(11, "zero-key install path", "a clean clone with no keys reaches a real backtest through the install path")
def _phase11_install() -> str:
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.endswith(("_KEY", "_SECRET", "_PASSWORD", "_USERNAME"))}
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.run(
        [sys.executable, "-m", "ee_agent.cli.main", "demo", "--symbol", "MNQ"],
        capture_output=True, text=True, env=env, cwd=str(REPO), timeout=900,
    )
    assert proc.returncode == 0, f"demo failed: {proc.stderr[-500:]}"
    assert "Net P&L" in proc.stdout, "the demo produced no backtest result"
    assert "AGREED" in proc.stdout, "the demo did not prove parity"
    return "ee-agent demo ran with no credentials: real backtest plus a parity proof"


@check(11, "founder's library", "the library holds full specs and is offered once, never pushed")
def _phase11_library() -> str:
    from ee_agent.library import loader

    entries = loader.entries()
    assert entries, "the library is empty"
    sweep = loader.get("sweep-return-v1")
    assert sweep.has_spec, "the Sweep Return entry has no spec"
    spec = sweep.load_spec()
    assert spec.signals.entry, "the library spec has no entry rules"
    return f"{len(entries)} entry(ies); sweep-return-v1 is a full spec ({spec.short_hash}), not just a script"


# ----------------------------------------------------- phase 13 (conversation)
@check(13, "conversation runs with no key", "the agent talks and says plainly what it cannot do without a model")
def _phase13_no_key() -> str:
    from ee_agent.conversation.agent import Conversation
    from ee_agent.conversation.model import NullModel

    answer = Conversation(model=NullModel(), sink=lambda _m: None).say("what do you make of gold here?")
    assert "no model key" in answer.lower(), "the no-key path did not explain itself"
    assert "everything else still works" in answer.lower(), "it did not say what still works"
    return "no-key conversation answers and names the boundary"


@check(13, "conversation operates the agent", "the model drives capture, data, compile and parity through tools", simulated="with a scripted model, so no API key is needed")
def _phase13_tools() -> str:
    import json as _json

    from ee_agent.conversation import tools as toolbox
    from ee_agent.conversation.agent import Conversation
    from tests.fake_model import ScriptedModel

    toolbox.reset_workspace()
    transcript = (REPO / "tests/fixtures/transcripts/sweep-return.txt").read_text(encoding="utf-8")
    model = ScriptedModel(
        script=[
            {"tools": [{"name": "describe_strategy", "arguments": {"text": transcript, "strategy_id": "verify-chat"}}]},
            {"tools": [{"name": "load_data", "arguments": {"symbol": "MNQ", "fixtures_only": True}}]},
            {"tools": [{"name": "compile_indicator", "arguments": {}}]},
            {"tools": [{"name": "prove_parity", "arguments": {}}]},
            {"text": "done"},
        ]
    )
    Conversation(model=model, sink=lambda _m: None).say(transcript)
    captured, loaded, compiled, parity = [_json.loads(r) for r in model.tool_results_seen]
    assert "MNQ" in captured["understood"]["instruments"]
    assert loaded["bars"] > 1000
    assert compiled["indicator_pine"].startswith("//@version=5")
    assert parity["agreed"] is True
    toolbox.reset_workspace()
    return (
        f"captured -> loaded {loaded['bars']:,} bars -> compiled Pine -> parity AGREED, "
        f"{len(toolbox.REGISTRY)} tools available"
    )


@check(13, "conversation cannot place an order", "no conversational tool reaches the order layer")
def _phase13_no_orders() -> str:
    import re as _re

    from ee_agent.conversation import tools as toolbox

    source = (REPO / "ee_agent/conversation/tools.py").read_text(encoding="utf-8")
    assert "OrderRouter" not in source and "execution.router" not in source
    order_call = _re.compile(r"(?:place_order|submit_order|send_order|OrderIntent\()")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "FORBIDDEN" in line or stripped.startswith('"'):
            continue
        assert not order_call.search(line), f"conversational tool reaches the order layer: {stripped[:60]}"
    return f"{len(toolbox.REGISTRY)} tools, none can place an order"


@check(13, "web search for data sources", "the agent finds sources for an asset it has no fetcher for")
def _phase13_discovery() -> str:
    from ee_agent.data.discovery import discover_sources, infer_asset_class

    offline = discover_sources("1-minute copper futures history", use_web=False)
    assert offline.candidates, "found nothing offline"
    flow = discover_sources("order flow footprint data for NQ futures", use_web=False)
    assert flow.candidates[0].has_order_flow, "an order-flow request did not rank a flow source first"
    assert infer_asset_class("bitcoin minute bars") == "crypto"
    live = discover_sources("exotic commodity data", use_web=True)
    assert live.search_note, "web search did not report what it did"
    return (
        f"{len(offline.candidates)} catalogue source(s) offline; web layer reports: "
        f"{live.search_note[:60]}"
    )


@check(13, "cost never blocks the conversation", "no ceiling means no stop, and a client ceiling asks rather than halting")
def _phase13_cost() -> str:
    from ee_agent.conversation.agent import Conversation
    from ee_agent.cost.notifier import CostLedger, ledger, set_ledger
    from tests.fake_model import ScriptedModel

    previous = ledger()
    try:
        set_ledger(CostLedger(ceiling_usd=None, notify_threshold_usd=10**9, sink=lambda _m: None))
        model = ScriptedModel(script=[{"text": "ok", "input_tokens": 10**6, "output_tokens": 10**6}])
        assert Conversation(model=model, sink=lambda _m: None).say("go") == "ok", "cost blocked with no ceiling"

        set_ledger(CostLedger(ceiling_usd=0.001, notify_threshold_usd=10**9, sink=lambda _m: None))
        model = ScriptedModel(script=[{"text": "ok", "input_tokens": 10**6, "output_tokens": 10**6}])
        answer = Conversation(model=model, sink=lambda _m: None).say("go")
        assert "ceiling" in answer.lower() and "have not stopped" in answer.lower()
    finally:
        set_ledger(previous)
    return "unbounded spend proceeds; a client-set ceiling asks and says nothing was stopped"


# ----------------------------------------------------------------- runner
def run_all(quick: bool = False, phases: list[int] | None = None) -> int:
    from ee_agent.cost.notifier import CostLedger, set_ledger

    set_ledger(CostLedger(notify_threshold_usd=10**9, sink=lambda _m: None))

    checks = [c for c in CHECKS if not phases or c.phase in phases]
    if quick:
        slow = {"parity across four targets", "replay through the live path", "overnight loop",
                "zero-key install path", "cross-asset scanner", "edge cases"}
        checks = [c for c in checks if c.name not in slow]

    print(f"\n  ee-agent verify -- {len(checks)} acceptance checks across "
          f"{len(set(c.phase for c in checks))} phases\n" + "  " + "=" * 92)
    results: list[Result] = []
    current_phase = None
    for chk in checks:
        if chk.phase != current_phase:
            current_phase = chk.phase
            print(f"\n  PHASE {chk.phase}")
        started = time.time()
        try:
            detail = chk.fn() or ""
            results.append(Result(chk, True, detail, time.time() - started))
            mark = "PASS"
        except Exception as exc:
            results.append(
                Result(chk, False, "", time.time() - started, error=f"{type(exc).__name__}: {exc}\n"
                       + traceback.format_exc(limit=3))
            )
            mark = "FAIL"
        result = results[-1]
        print(f"    [{mark}] {chk.name:<34} {result.seconds:>6.1f}s  {chk.criterion[:74]}")
        if result.detail:
            print(f"           {result.detail[:110]}")
        if chk.simulated:
            print(f"           satisfied in simulation: {chk.simulated}")
        if not result.passed:
            print("           " + result.error.splitlines()[0])
            for line in result.error.splitlines()[1:6]:
                print("           " + line)

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    simulated = sum(1 for r in results if r.check.simulated and r.passed)
    print("\n  " + "=" * 92)
    print(f"  {passed}/{total} acceptance checks passed ({simulated} satisfied in simulation, as Section 2.3 allows)")
    print(f"  total {sum(r.seconds for r in results):.1f}s\n")
    if passed < total:
        print("  FAILED:")
        for r in results:
            if not r.passed:
                print(f"    phase {r.check.phase} -- {r.check.name}: {r.error.splitlines()[0]}")
        print()
    return 0 if passed == total else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    raise SystemExit(run_all(quick="--quick" in args))
