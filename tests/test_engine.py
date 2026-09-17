"""The truth engine: costs, fills, lookahead, walk-forward, Monte Carlo, prop rules.

The parity harness and the position ledger get the most thorough tests in the
codebase (Section 2.4) -- those live in test_parity.py and test_execution.py.
This file covers everything that decides what a reported number means.
"""
from __future__ import annotations

import numpy as np
import pytest

from ee_agent.engine.analysis import (
    correct_for_selection, monte_carlo, run_walk_forward, walk_forward_windows,
)
from ee_agent.engine.backtester import Backtester
from ee_agent.engine.costs import CostModel
from ee_agent.engine.fills import FillModel
from ee_agent.engine.truth import TruthEngine
from ee_agent.errors import CostModelMissing
from ee_agent.instruments.registry import Instrument, get_instrument
from ee_agent.prop.rules import daily_pnls_from_trades, load_firm, simulate_combine
from ee_agent.spec.model import Condition, Costs, SignalRule


# ----------------------------------------------------------------- costs
def test_backtest_without_costs_is_impossible(spec, small_bars):
    spec.costs = Costs(commission_per_side=0, slippage_ticks=0, spread_ticks=0, exchange_fees_per_side=0)
    zero_fee = Instrument(symbol="ZERO", name="zero", asset_class="future", tick_size=0.25, tick_value=0.5)
    with pytest.raises(CostModelMissing):
        Backtester(spec, instrument=zero_fee).run(small_bars)


def test_costs_are_charged_on_every_trade(spec, small_bars):
    result = Backtester(spec).run(small_bars)
    assert result.costs_total > 0
    for trade in result.trades:
        if trade.closed:
            assert trade.commission > 0
            assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.commission, rel=1e-9)


def test_slippage_grows_with_participation_and_volatility(spec):
    instrument = get_instrument("MNQ")
    model = CostModel.from_spec(spec.costs, instrument)
    small = model.slippage_for(size=1, bar_volume=10000, atr_ticks=10, typical_volume=900000)
    large = model.slippage_for(size=5000, bar_volume=10000, atr_ticks=10, typical_volume=900000)
    volatile = model.slippage_for(size=1, bar_volume=10000, atr_ticks=200, typical_volume=900000)
    assert large > small
    assert volatile > small


def test_stop_orders_pay_more_slippage(spec):
    model = CostModel.from_spec(spec.costs, get_instrument("MNQ"))
    normal = model.slippage_for(1, 10000, 10, 900000, is_stop=False)
    stop = model.slippage_for(1, 10000, 10, 900000, is_stop=True)
    assert stop > normal


# ----------------------------------------------------------------- fills
def test_ambiguous_bar_resolves_to_the_stop(spec):
    model = FillModel(CostModel.from_spec(spec.costs, get_instrument("MNQ")))
    verdict = model.ambiguous_bar_resolution("long", stop_price=99.0, target_price=101.0,
                                             bar_high=102.0, bar_low=98.0)
    assert verdict == "both_in_bar:assumed_stop_first"


def test_gap_through_a_stop_fills_at_the_open(spec):
    model = FillModel(CostModel.from_spec(spec.costs, get_instrument("MNQ")))
    fill = model.stop("long", stop_price=100.0, size=1, bar_open=95.0, bar_high=96.0, bar_low=94.0,
                      bar_volume=5000, atr_ticks=10)
    assert fill is not None
    assert "gap" in fill.reason
    assert fill.price < 100.0, "a gapped stop filled at or better than the stop price"


def test_limit_needs_more_than_a_touch(spec):
    model = FillModel(CostModel.from_spec(spec.costs, get_instrument("MNQ")))
    touched = model.limit("short", limit_price=100.0, size=500, bar_high=100.0, bar_low=99.0, bar_volume=100)
    assert touched is None, "a limit filled on a touch with no volume behind it"
    through = model.limit("short", limit_price=100.0, size=1, bar_high=101.0, bar_low=99.0, bar_volume=5000)
    assert through is not None


def test_partial_fill_when_the_bar_is_thin(spec):
    model = FillModel(CostModel.from_spec(spec.costs, get_instrument("MNQ")))
    fill = model.market("long", reference_price=100.0, size=1000, bar_volume=100, atr_ticks=10)
    assert fill.partial and fill.size < 1000


# ------------------------------------------------------------- backtester
def test_no_fill_is_better_than_the_bar_allowed(spec, small_bars):
    from ee_agent.engine.lookahead import check_fills

    result = Backtester(spec).run(small_bars)
    assert check_fills(result, small_bars).impossible_fills == []


def test_max_trades_per_day_is_respected(spec, small_bars):
    spec.filters.max_trades_per_day = 1
    result = Backtester(spec).run(small_bars)
    by_day: dict[str, int] = {}
    for trade in result.trades:
        day = str(small_bars.local_date[trade.entry_index])
        by_day[day] = by_day.get(day, 0) + 1
    assert max(by_day.values(), default=0) <= 1


def test_max_concurrent_positions_is_respected(spec, small_bars):
    result = Backtester(spec).run(small_bars)
    events = []
    for trade in result.trades:
        events.append((trade.entry_index, 1))
        if trade.exit_index is not None:
            events.append((trade.exit_index, -1))
    open_count = 0
    for _index, delta in sorted(events):
        open_count += delta
        assert open_count <= spec.risk.max_concurrent_positions


def test_same_spec_runs_on_two_asset_classes(spec, small_bars, crypto_bars):
    futures = Backtester(spec).run(small_bars)
    crypto = Backtester(spec).run(crypto_bars.slice(0, 3000))
    assert futures.metrics.n_trades > 0 and crypto.metrics.n_trades > 0


def test_results_are_reproducible(spec, small_bars):
    a = Backtester(spec).run(small_bars)
    b = Backtester(spec).run(small_bars)
    assert a.metrics.to_dict() == b.metrics.to_dict()


# ---------------------------------------------------------------- analysis
def test_walk_forward_splits_in_and_out_of_sample(spec, small_bars):
    engine = Backtester(spec)
    result = run_walk_forward(
        lambda start, end, label: engine.run(small_bars.slice(start, end), label), len(small_bars), n_windows=4
    )
    assert result.windows
    assert result.in_sample is not None and result.out_of_sample is not None


def test_walk_forward_windows_do_not_overlap_train_and_test():
    for window in walk_forward_windows(10000, n_windows=5):
        assert window.train_end <= window.test_start


def test_monte_carlo_produces_a_band():
    pnls = [100, -50, 75, -120, 200, -30, 45, -80, 150, -60] * 5
    result = monte_carlo(pnls, n_paths=500, seed=1)
    assert result.drawdown_p95 >= result.drawdown_p50
    assert result.final_p95 > result.final_p05
    assert 0 <= result.prob_profitable <= 1


def test_selection_correction_shrinks_the_winner():
    corrected = correct_for_selection(best_sharpe=2.0, n_trials=200, n_observations=100)
    assert corrected.corrected_metric < corrected.best_metric


def test_lookahead_detector_catches_a_peeking_strategy(spec, small_bars):
    import numpy as np

    from ee_agent.compile.to_python import compile_to_python
    from ee_agent.engine import lookahead as la
    from ee_agent.spec import primitives as prim

    class PeekNode(prim.Node):
        def prepare(self, rt):
            close = rt.bars.close
            self.series = np.concatenate((close[1:], [close[-1]])) > close

    if "test_peek" not in prim.REGISTRY:
        prim.register(
            prim.Primitive(
                name="test_peek", kind="condition", doc="TEST ONLY", params={},
                python=lambda p, env: PeekNode(p, env),
                pine_indicator=lambda p, a, e: prim.PineFragment(expr="close[-1] > close"),
                pine_strategy=lambda p, a, e: prim.PineFragment(expr="close[-1] > close"),
                live=lambda p, e: {"type": "test_peek"},
            )
        )
    spec.signals.entry = [SignalRule(id="long", side="long", all_of=[Condition("test_peek", {})])]
    report = la.detect(compile_to_python(spec), small_bars, sample=10, min_bars=400)
    assert not report.clean
    assert report.divergences


def test_clean_strategy_passes_the_lookahead_detector(spec, small_bars):
    from ee_agent.compile.to_python import compile_to_python
    from ee_agent.engine import lookahead as la

    report = la.detect(compile_to_python(spec), small_bars, sample=12, min_bars=400)
    assert report.clean, report.summary()


# -------------------------------------------------------------- prop rules
def test_every_rule_pack_loads():
    from ee_agent.prop.rules import available_firms

    for firm in available_firms():
        rules = load_firm(firm)
        assert rules.accounts
        for account in rules.accounts:
            assert account.profit_target > 0
            assert account.trailing_max_drawdown > 0


def test_combine_simulation_answers_the_three_questions():
    daily = [400.0, -250.0, 180.0, 520.0, -150.0, 90.0]
    report = simulate_combine(daily, firm="topstep", n_runs=300, seed=3)
    assert 0 <= report.pass_rate <= 1
    assert 0 <= report.blowup_rate <= 1
    assert report.pass_rate + report.blowup_rate + report.timeout_rate == pytest.approx(1.0, abs=1e-9)


def test_a_losing_strategy_never_passes_a_combine():
    report = simulate_combine([-300.0] * 10, firm="topstep", n_runs=200, seed=4)
    assert report.pass_rate == 0.0
    assert report.blowup_rate > 0.5


def test_intraday_swing_makes_the_daily_limit_bite():
    daily = [-900.0, 400.0, 300.0, 200.0]
    gentle = simulate_combine(daily, firm="topstep", n_runs=300, seed=5, intraday_swing_factor=1.0)
    harsh = simulate_combine(daily, firm="topstep", n_runs=300, seed=5, intraday_swing_factor=2.0)
    assert harsh.blowup_rate >= gentle.blowup_rate


def test_end_of_day_trailing_is_more_forgiving_than_intraday():
    daily = [250.0, -180.0, 300.0, -120.0, 400.0]
    intraday = simulate_combine(daily, firm="topstep", n_runs=400, seed=6)
    end_of_day = simulate_combine(daily, firm="takeprofit", n_runs=400, seed=6)
    assert end_of_day.blowup_rate <= intraday.blowup_rate + 0.25


# ------------------------------------------------------------ truth engine
def test_report_is_not_defensible_without_the_full_analysis(spec, small_bars):
    single = Backtester(spec).run(small_bars)
    assert single.defensible is False
    assert "SINGLE WINDOW" in single.report()


def test_full_analysis_is_defensible(spec, bars):
    report = TruthEngine(spec, monte_carlo_paths=100, synthetic_paths=1).analyze(bars.slice(0, 6000), pin=False)
    assert report.defensible
    text = report.report()
    for required in ("[walk-forward]", "[monte carlo]", "[lookahead]", "[combine]", "[adversarial]"):
        assert required in text


def test_adversarial_pass_attacks_a_weak_result(spec, bars):
    report = TruthEngine(spec, monte_carlo_paths=100, synthetic_paths=1).analyze(bars.slice(0, 6000), pin=False)
    assert report.adversarial is not None
    assert report.adversarial.verdict
    assert isinstance(report.adversarial.survived, bool)


def test_artifacts_are_pinned_and_rerunnable(spec, bars, tmp_path):
    report = TruthEngine(spec, monte_carlo_paths=50, synthetic_paths=0).analyze(bars.slice(0, 3000), pin=False)
    out = report.pin(root=tmp_path)
    for name in ("spec.yaml", "report.json", "trades.json", "pin.json"):
        assert (out / name).exists()
    import json

    pin = json.loads((out / "pin.json").read_text(encoding="utf-8"))
    assert pin["spec_hash"] == spec.hash
