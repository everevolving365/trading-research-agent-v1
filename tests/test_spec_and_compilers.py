"""Spec, primitives, validator, portability and the four compilers."""
from __future__ import annotations

import json

import pytest

from ee_agent.compile.to_live import compile_to_live
from ee_agent.compile.to_pine import compile_to_pine_indicator, compile_to_pine_strategy, pine_limitations
from ee_agent.compile.to_python import compile_to_python, emit_script
from ee_agent.errors import PrimitiveError, SpecError
from ee_agent.spec import primitives as prim
from ee_agent.spec.diff import diff
from ee_agent.spec.model import Condition, Distance, SignalRule, StrategySpec
from ee_agent.spec.portability import is_portable, to_atr_units
from ee_agent.spec.validator import validate


# ------------------------------------------------------------------- spec
def test_spec_round_trips_through_yaml(spec):
    again = StrategySpec.from_yaml(spec.to_yaml())
    assert again.hash == spec.hash
    assert again.to_dict() == spec.to_dict()


def test_spec_hash_changes_with_content(spec):
    before = spec.hash
    spec.risk.stop = Distance("atr", 99.0)
    assert spec.hash != before


def test_spec_describe_is_readable(spec):
    text = spec.describe()
    assert spec.name in text and "Stop" in text and "Costs" in text


def test_diff_reports_each_change(spec):
    other = StrategySpec.from_dict(spec.to_dict())
    other.risk.stop = Distance("atr", 1.0)
    other.filters.max_trades_per_day = 5
    result = diff(spec, other)
    assert len(result.changes) >= 2
    assert "risk.stop" in result.summary()


# ------------------------------------------------------------- primitives
def test_every_primitive_implements_all_four_targets():
    assert prim.audit_registry() == []
    assert len(prim.REGISTRY) >= 6


def test_unknown_primitive_raises_with_guidance():
    with pytest.raises(PrimitiveError) as exc:
        prim.get("does_not_exist")
    assert "all four targets" in str(exc.value)


def test_primitives_are_causal(spec, small_bars):
    """Truncating the series must not change any earlier signal."""
    compiled = compile_to_python(spec)
    full = compiled.evaluate(small_bars)
    cut = 2000
    partial = compiled.evaluate(small_bars.slice(0, cut))
    assert (full.long_entry[:cut] == partial.long_entry).all()
    assert (full.short_entry[:cut] == partial.short_entry).all()


# -------------------------------------------------------------- validator
def test_validator_accepts_a_good_spec(spec):
    report = validate(spec)
    assert report.ok, report


def test_validator_blocks_a_frictionless_spec(spec):
    spec.costs.commission_per_side = 0
    spec.costs.slippage_ticks = 0
    spec.costs.spread_ticks = 0
    report = validate(spec)
    assert any(f.code == "COST001" for f in report.blocking)


def test_validator_blocks_missing_stop(spec):
    spec.risk.stop = None
    assert any(f.code == "RISK001" for f in validate(spec).blocking)


def test_validator_rejects_currency_units(spec):
    spec.risk.stop = Distance("dollars", 100)  # type: ignore[arg-type]
    assert any(f.code == "RISK010" for f in validate(spec).errors)


def test_validator_blocks_unapproved_assumptions(spec):
    from ee_agent.spec.model import Assumption

    spec.assumptions.append(Assumption(id="x", question="q?", resolution="r", approved_by="pending"))
    report = validate(spec)
    assert any(f.code == "AMB001" for f in report.blocking)
    assert not report.live_ready


def test_validator_rejects_unknown_reference(spec):
    spec.signals.entry[0].all_of[0].params["reference"] = "nope"
    assert any(f.code == "PRIM005" for f in validate(spec).errors)


def test_report_raises_on_errors(spec):
    spec.universe.instruments = []
    with pytest.raises(SpecError):
        validate(spec).raise_if_bad()


# ------------------------------------------------------------ portability
def test_owner_spec_is_flagged_as_not_portable(owner_spec):
    assert not is_portable(owner_spec)
    report = validate(owner_spec)
    assert any(f.code == "PORT001" for f in report.findings)


def test_atr_conversion_is_unapproved_until_the_client_approves(owner_spec, bars):
    proposed, rows = to_atr_units(owner_spec, bars)
    assert rows and is_portable(proposed)
    assert proposed.unresolved_assumptions(), "a conversion was applied without approval"
    approved, _ = to_atr_units(owner_spec, bars, approved_by="owner")
    assert not approved.unresolved_assumptions()


def test_atr_spec_is_portable(spec):
    assert is_portable(spec)


# -------------------------------------------------------------- compilers
def test_all_four_targets_share_one_plan_hash(spec):
    python = compile_to_python(spec)
    indicator = compile_to_pine_indicator(spec)
    strategy = compile_to_pine_strategy(spec)
    live = compile_to_live(spec)
    assert python.plan_hash == indicator.plan_hash == strategy.plan_hash == live.plan_hash


def test_pine_indicator_has_arrows_and_alerts(spec):
    source = compile_to_pine_indicator(spec).source
    assert source.startswith("//@version=5")
    assert "plotshape(" in source
    assert "alertcondition(" in source
    assert "alert(" in source


def test_pine_strategy_carries_costs_and_risk(spec):
    source = compile_to_pine_strategy(spec).source
    assert "commission_value=" in source
    assert "slippage=" in source
    assert "strategy.entry" in source
    assert "strategy.exit" in source


def test_pine_uses_mintick_not_hardcoded_prices(spec):
    source = compile_to_pine_strategy(spec).source
    assert "syminfo.mintick" in source or "ta.atr(" in source


def test_live_config_verifies_alert_hashes(spec):
    from ee_agent.execution.live_plan import LivePlan

    config = compile_to_live(spec)
    plan = LivePlan(config.to_dict())
    ok, _ = plan.verify_alert({"spec_hash": spec.hash, "plan_hash": config.plan_hash, "side": "long"})
    assert ok
    bad, why = plan.verify_alert({"spec_hash": "sha256:stale", "side": "long"})
    assert not bad and "not the strategy" in why


def test_emitted_script_is_deterministic(spec):
    assert emit_script(spec) == emit_script(spec)


def test_flow_primitive_limitation_is_stated(spec):
    spec.signals.entry = [
        SignalRule(id="long", side="long", all_of=[Condition("cum_delta_cross", {"length": 20})])
    ]
    notes = pine_limitations(spec)
    assert notes and "proxy" in notes[0]


def test_no_model_in_the_compile_path():
    from pathlib import Path

    import ee_agent.compile.to_live as m1
    import ee_agent.compile.to_pine as m2
    import ee_agent.compile.to_python as m3

    for module in (m1, m2, m3):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for banned in ("anthropic", "openai", "genai", "requests", "httpx"):
            assert f"import {banned}" not in source
