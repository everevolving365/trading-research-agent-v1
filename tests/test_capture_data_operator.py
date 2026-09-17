"""Capture, data, the Operator, the sandbox, flow, research and the ledger."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent


# ================================================================== capture
def test_transcript_produces_a_complete_spec():
    from ee_agent.capture.interrogation import InterrogationEngine
    from ee_agent.capture.parser import parse
    from ee_agent.spec.validator import validate

    text = (REPO / "tests/fixtures/transcripts/sweep-return.txt").read_text(encoding="utf-8")
    parsed = parse(text, strategy_id="captured")
    assert "MNQ" in parsed.found["instruments"]
    assert parsed.found["opening_range"] == "08:30-09:30"
    assert parsed.found["signal_window"] == "09:30-15:00"
    spec = InterrogationEngine(parsed.spec, sink=lambda _m: None).run(lambda _q: None)
    assert validate(spec).ok
    assert not spec.unresolved_assumptions()


def test_stop_and_target_are_not_confused():
    """'my stop is 25 points, target is 50' must not give the target the 25."""
    from ee_agent.capture.parser import parse

    parsed = parse("my stop is 25 points, that's always been it. target is 50 points. one contract.")
    assert parsed.spec.risk.stop.value == 25
    assert parsed.spec.risk.target.value == 50


def test_negation_is_understood():
    from ee_agent.capture.parser import parse

    assert parse("I don't scale in, ever.").spec.risk.scale_in is False
    assert parse("I scale in once it moves my way.").spec.risk.scale_in is True


def test_word_numbers_are_understood():
    from ee_agent.capture.parser import parse

    assert parse("I trade one contract at a time").spec.risk.size.value == 1


def test_afternoon_times_are_interpreted_as_pm():
    from ee_agent.capture.parser import find_time_windows

    windows = find_time_windows("from 9:30 to 3:00 is my window")
    assert windows[0] == ("09:30", "15:00", "9:30 to 3:00")


def test_interrogation_asks_about_what_is_missing():
    from ee_agent.capture.interrogation import InterrogationEngine
    from ee_agent.capture.parser import parse

    parsed = parse("I buy when the 9 ema crosses the 21 ema on MNQ")
    engine = InterrogationEngine(parsed.spec, sink=lambda _m: None)
    topics = {q.topic for q in engine.state.outstanding}
    assert "stop_placement" in topics and "target_logic" in topics


def test_unanswered_questions_become_unapproved_assumptions():
    from ee_agent.capture.interrogation import InterrogationEngine
    from ee_agent.capture.parser import parse

    parsed = parse("I buy when the 9 ema crosses the 21 ema on MNQ")
    engine = InterrogationEngine(parsed.spec, sink=lambda _m: None)
    spec = engine.run(lambda _q: None)
    assumed = [a for a in spec.assumptions if not a.approved]
    assert assumed, "silence produced no unapproved assumption"
    assert all(a.sensitivity or a.alternatives for a in spec.assumptions)


def test_answers_are_applied_to_the_spec():
    from ee_agent.capture.interrogation import InterrogationEngine
    from ee_agent.capture.parser import parse

    engine = InterrogationEngine(parse("I trade MNQ").spec, sink=lambda _m: None)
    assert engine.apply("stop_placement", "1.5 ATR")
    assert engine.spec.risk.stop.type == "atr" and engine.spec.risk.stop.value == 1.5
    assert engine.apply("max_trades_per_day", "three")
    assert engine.spec.filters.max_trades_per_day == 3
    assert engine.apply("overlapping_signals", "just ignore it")
    assert engine.spec.execution.on_overlapping_signal == "ignore_new"


def test_sensitivity_is_measured_not_just_logged(spec, small_bars):
    from ee_agent.capture.interrogation import measure_sensitivity

    result = measure_sensitivity(spec, small_bars, "session_close_behavior", "hold")
    assert "difference" in result
    assert "measured" in result


def test_visual_loop_removes_a_consistently_rejected_side(spec, small_bars, tmp_path):
    from ee_agent.capture.visual import SyntheticApprover, run_visual_loop

    result = run_visual_loop(
        spec, small_bars, SyntheticApprover(REPO / "tests/fixtures/approvals/reject-longs.json"),
        out_dir=tmp_path, limit=10, render=False,
    )
    assert result.revisions
    assert "long" not in {r.side or r.id for r in result.spec.signals.entry}


def test_visual_loop_moves_the_window_when_early_examples_are_rejected(spec, small_bars, tmp_path):
    from ee_agent.capture.visual import SyntheticApprover, run_visual_loop

    result = run_visual_loop(
        spec, small_bars, SyntheticApprover(REPO / "tests/fixtures/approvals/reject-early.json"),
        out_dir=tmp_path, limit=10, render=False,
    )
    assert result.spec.filters.time_windows[0].start > spec.filters.time_windows[0].start


def test_visual_revisions_are_recorded_as_assumptions(spec, small_bars, tmp_path):
    from ee_agent.capture.visual import SyntheticApprover, run_visual_loop

    result = run_visual_loop(
        spec, small_bars, SyntheticApprover(REPO / "tests/fixtures/approvals/reject-longs.json"),
        out_dir=tmp_path, limit=10, render=False,
    )
    assert any(a.id.startswith("visual:") for a in result.spec.assumptions)


def test_ascii_chart_works_with_no_plotting_library(spec, small_bars):
    from ee_agent.capture.visual import ascii_chart, find_candidates

    candidates = find_candidates(spec, small_bars, limit=2)
    assert candidates
    chart = ascii_chart(candidates[0])
    assert "signal bar" in chart and "#" in chart


def test_natural_language_edit_changes_one_thing(spec):
    from ee_agent.capture.intake import edit

    result = edit(spec, "only trade shorts")
    assert result.applied
    assert {r.side or r.id for r in result.after.signals.entry} == {"short"}


def test_unrecognised_edit_changes_nothing(spec):
    from ee_agent.capture.intake import edit

    result = edit(spec, "make it better somehow")
    assert not result.applied
    assert result.after.hash == spec.hash
    assert "changed nothing" in result.reason


def test_before_and_after_shows_both_results(spec, small_bars):
    from ee_agent.capture.intake import before_and_after

    text = before_and_after(spec, "make the stop 3 ATR", small_bars)
    assert "BEFORE" in text and "AFTER" in text and "net P&L" in text


def test_fill_history_reconstruction():
    from ee_agent.capture.intake import reconstruct_from_fills, spec_from_fills

    rows = []
    stamp = pd.Timestamp("2026-03-02 09:35", tz="UTC")
    for day in range(6):
        for trade in range(2):
            entry = stamp + pd.Timedelta(days=day, minutes=trade * 45)
            rows.append({"ts": entry, "symbol": "MNQ", "side": "buy", "qty": 1, "price": 20000})
            rows.append({"ts": entry + pd.Timedelta(minutes=12), "symbol": "MNQ", "side": "sell",
                         "qty": 1, "price": 20000 + (25 if trade == 0 else -12)})
    behaviour = reconstruct_from_fills(pd.DataFrame(rows), tick_size=0.25)
    assert behaviour.n_round_turns == 12
    assert behaviour.median_target_ticks and behaviour.median_stop_ticks
    spec = spec_from_fills(behaviour)
    assert spec.unresolved_assumptions(), "inferred rules were treated as approved"
    assert any("entry_rule" in a.id for a in spec.assumptions)


def test_screenshot_intake_is_honest_without_a_vision_model(tmp_path):
    from ee_agent.capture.intake import read_screenshots

    path = tmp_path / "MNQ_5m_2026-03-02.png"
    path.write_bytes(b"not really a png")
    hints = read_screenshots([path])
    assert hints[0].symbol == "MNQ" and hints[0].timeframe == "5m"
    assert "vision model" in hints[0].note


def test_voice_is_optional():
    from ee_agent.capture.voice import Speaker, VoiceShell, status

    assert isinstance(status().summary(), str)
    speaker = Speaker(enabled=False)
    speaker.say("hello")
    assert speaker.spoken == ["hello"], "the transcript is lost when voice is off"
    shell = VoiceShell(enabled=False, input_fn=lambda _p: "an answer")
    assert shell.ask("question?") == "an answer"


# ===================================================================== data
def test_every_load_prints_cache_age_and_last_bar():
    from ee_agent.data.loader import load_bars

    printed: list[str] = []
    load_bars("MNQ", "5m", fixtures_only=True, use_cache=False, sink=printed.append)
    joined = "\n".join(printed)
    assert "cache age" in joined and "last bar" in joined and "quality" in joined


def test_integrity_engine_catches_every_defect():
    from ee_agent.data.integrity import check_and_clean
    from ee_agent.data.synthetic import (
        GenSpec, generate, inject_duplicates, inject_gap, inject_out_of_order, inject_zero_volume,
    )

    base = generate(GenSpec(symbol="MNQ", timeframe="5m", days=6, seed=42))
    _c, gapped = check_and_clean(inject_gap(base, 30, 10))
    assert gapped.gaps
    _c, duped = check_and_clean(inject_duplicates(base, 20, 4))
    assert duped.duplicates_removed == 4
    _c, unordered = check_and_clean(inject_out_of_order(base, 40))
    assert unordered.reordered > 0
    _c, zeros = check_and_clean(inject_zero_volume(base, 50, 5))
    assert zeros.zero_volume_bars >= 5


def test_quality_score_falls_with_defects():
    from ee_agent.data.integrity import check_and_clean
    from ee_agent.data.synthetic import GenSpec, generate, inject_gap

    base = generate(GenSpec(symbol="MNQ", timeframe="5m", days=6, seed=43))
    _clean, good = check_and_clean(base)
    _dirty, bad = check_and_clean(inject_gap(base, 30, 60))
    assert bad.score < good.score


def test_dst_transitions_are_detected():
    from ee_agent.data.integrity import check_and_clean
    from ee_agent.data.synthetic import dst_window

    _bars, report = check_and_clean(dst_window())
    assert report.dst_transitions


def test_continuous_futures_stitching():
    from ee_agent.data.integrity import stitch_continuous
    from ee_agent.data.synthetic import GenSpec, generate

    front = generate(GenSpec(symbol="MNQ", timeframe="5m", days=5, seed=50, start_price=20000))
    back = generate(GenSpec(symbol="MNQ", timeframe="5m", days=5, seed=51, start_price=20120))
    stitched, notes = stitch_continuous([("Z25", front), ("H26", back)])
    assert notes and len(stitched) > 0


def test_splits_are_back_adjusted_only_before_the_date():
    from ee_agent.data.integrity import adjust_splits_dividends
    from ee_agent.data.synthetic import GenSpec, generate

    bars = generate(GenSpec(symbol="SPY", timeframe="5m", days=6, seed=60, start_price=500))
    split_day = bars.df["ts"].iloc[len(bars) // 2].strftime("%Y-%m-%d")
    adjusted, notes = adjust_splits_dividends(bars, splits=[(split_day, 2.0)])
    assert notes
    assert adjusted.close[0] < bars.close[0]
    assert adjusted.close[-1] == pytest.approx(bars.close[-1])


def test_resample_reconstructs_higher_timeframes():
    from ee_agent.data.loader import load_bars

    bars = load_bars("MNQ", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None).slice(0, 600)
    hourly = bars.resample("1h")
    assert 0 < len(hourly) < len(bars)
    assert hourly.high.max() == pytest.approx(bars.high.max())


def test_ingestion_infers_an_unknown_schema():
    from ee_agent.data.ingest import ingest_dataframe

    df = pd.DataFrame(
        {
            "Gmt time": pd.date_range("2026-02-02 09:30", periods=200, freq="5min").strftime("%d.%m.%Y %H:%M:%S"),
            "Open": range(100, 300), "High": range(101, 301), "Low": range(99, 299),
            "Close": range(100, 300), "Volume": [5] * 200,
        }
    )
    result = ingest_dataframe(df, symbol="MNQ")
    assert result.bars is not None and len(result.bars) == 200


def test_ingestion_recognises_a_broker_export():
    from ee_agent.data.ingest import ingest_dataframe

    df = pd.DataFrame(
        {
            "Filled at": pd.date_range("2026-02-02 09:30", periods=8, freq="30min"),
            "Instrument": ["MNQ"] * 8,
            "B/S": ["Buy", "Sell"] * 4,
            "Filled Qty": [1] * 8,
            "Avg Price": [20000, 20025] * 4,
        }
    )
    result = ingest_dataframe(df)
    assert result.fills is not None and result.kind == "broker fill history"
    assert set(result.fills["side"].unique()) <= {"buy", "sell"}


def test_source_ladder_prefers_available_sources():
    from ee_agent.data.source_registry import ladder

    chain = ladder("crypto", "5m", 30)
    assert "binance" in chain
    assert chain.index("binance") < chain.index("fixture") or "fixture" not in chain


def test_paywall_notice_states_the_fallback_cost():
    from ee_agent.data.paywall import notice_for

    notice = notice_for("databento")
    message = notice.message()
    assert "paywall" in message.lower()
    assert "order flow" in message
    assert "ee-agent secrets set DATABENTO_API_KEY" in message


# ===================================================================== flow
def test_flow_degrades_and_says_so():
    from ee_agent.data.bars import Bars
    from ee_agent.data.loader import load_bars
    from ee_agent.flow.primitives import cumulative_delta, flow_report

    bars = load_bars("MNQ", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None).slice(0, 2000)
    assert bars.has_flow
    _cd, degraded = cumulative_delta(bars)
    assert not degraded

    stripped = bars.df.drop(columns=[c for c in ("bid_volume", "ask_volume", "delta", "trades") if c in bars.df.columns])
    plain = Bars(symbol="MNQ", timeframe="5m", df=stripped, tz=bars.tz, source="stripped")
    _cd2, degraded2 = cumulative_delta(plain)
    assert degraded2
    assert "proxy" in flow_report(plain)["note"]


def test_volume_profile_finds_a_point_of_control():
    from ee_agent.data.loader import load_bars
    from ee_agent.flow.primitives import volume_profile

    bars = load_bars("MNQ", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None).slice(0, 800)
    profile = volume_profile(bars, tick_size=0.25)
    assert profile.value_area_low <= profile.poc <= profile.value_area_high


def test_vwap_bands_are_causal():
    from ee_agent.data.loader import load_bars
    from ee_agent.flow.primitives import vwap_bands

    bars = load_bars("MNQ", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None).slice(0, 500)
    bands = vwap_bands(bars)
    full = bands["vwap"]
    partial = vwap_bands(bars.slice(0, 300))["vwap"]
    assert np.allclose(full[:300], partial, equal_nan=True)


# ================================================================= operator
def test_operator_module_contains_no_order_placement():
    import re

    order_call = re.compile(
        r"\b(?:place_order|submit_order|send_order|market_order|limit_order|strategy\.entry|\.buy\(|\.sell\()", re.I
    )
    for path in (REPO / "ee_agent/operator").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "OrderIntent" not in source, f"{path.name} imports the order layer"
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "FORBIDDEN_ACTIONS" in line or stripped.startswith('"'):
                continue
            assert not order_call.search(line), f"{path.name}: {stripped[:70]}"


def test_operator_refuses_an_order_action():
    from ee_agent.errors import OperatorRefusal
    from ee_agent.operator.browser import AuditTrail, MockPageDriver

    audit = AuditTrail(root=Path(__import__("tempfile").mkdtemp()))
    driver = MockPageDriver(REPO / "tests/fixtures/tradingview")
    with pytest.raises(OperatorRefusal):
        audit.record("buy", "MNQ", driver=driver)


def test_full_tradingview_flow_against_the_mock(monkeypatch, tmp_path, spec):
    from ee_agent.compile.to_live import compile_to_live
    from ee_agent.compile.to_pine import compile_to_pine_indicator, compile_to_pine_strategy
    from ee_agent.operator.browser import AuditTrail, MockPageDriver
    from ee_agent.operator.tradingview import TradingViewOperator

    monkeypatch.setenv("TRADINGVIEW_USERNAME", "u")
    monkeypatch.setenv("TRADINGVIEW_PASSWORD", "p")
    operator = TradingViewOperator(
        MockPageDriver(REPO / "tests/fixtures/tradingview"), AuditTrail(root=tmp_path)
    )
    result = operator.full_flow(
        compile_to_pine_indicator(spec), compile_to_pine_strategy(spec), compile_to_live(spec),
        webhook_url="https://example.invalid/hook", deep_range=("2018-01-01", "2026-06-30"),
    )
    assert result.ok, result.errors
    assert result.report.deep and result.report.total_trades == 214
    assert result.alerts_created == 2
    assert (tmp_path / operator.audit.dir.name / "audit.json").exists() or (operator.audit.dir / "audit.json").exists()


def test_reading_chart_trades_is_refused(spec):
    from ee_agent.errors import OperatorRefusal
    from ee_agent.operator.browser import AuditTrail, MockPageDriver
    from ee_agent.operator.tradingview import TradingViewOperator

    operator = TradingViewOperator(
        MockPageDriver(REPO / "tests/fixtures/tradingview"),
        AuditTrail(root=Path(__import__("tempfile").mkdtemp())),
    )
    with pytest.raises(OperatorRefusal) as exc:
        operator.read_chart_trades()
    assert "REGULAR backtest" in str(exc.value)


def test_publishing_requires_explicit_confirmation():
    from ee_agent.errors import OperatorRefusal
    from ee_agent.operator.browser import AuditTrail, MockPageDriver
    from ee_agent.operator.tradingview import TradingViewOperator

    operator = TradingViewOperator(
        MockPageDriver(REPO / "tests/fixtures/tradingview"),
        AuditTrail(root=Path(__import__("tempfile").mkdtemp())),
    )
    with pytest.raises(OperatorRefusal):
        operator.publish_script("my script")
    assert operator.publish_script("my script", confirm=True)


def test_report_parser_handles_both_report_kinds():
    from ee_agent.operator.tradingview import parse_report

    regular = parse_report("Net Profit 612.50 USD 1.23%\nTotal Closed Trades 28\nProfit Factor 1.11\n")
    assert regular.total_trades == 28 and not regular.deep
    deep = parse_report("Net Profit 4,812.50 USD 9.63%\nTotal Closed Trades 214\n"
                        "Deep Backtesting 2018-01-01 to 2026-06-30\n")
    assert deep.deep and deep.total_trades == 214 and deep.net_profit == 4812.50


def test_cross_check_reports_the_gap(spec, bars):
    from ee_agent.engine.truth import TruthEngine
    from ee_agent.operator.tradingview import cross_check, parse_report

    report = TruthEngine(spec, monte_carlo_paths=50, synthetic_paths=0).analyze(bars.slice(0, 3000), pin=False)
    tv = parse_report("Net Profit 1,000.00 USD 2.00%\nTotal Closed Trades 50\nCommission Paid 100.00 USD\n")
    result = cross_check(report, tv)
    assert "authority" in result and "Python engine is authoritative" in result["authority"]
    assert result["explanations"]


# ================================================================== sandbox
def test_sandbox_blocks_network_secrets_and_stray_writes(monkeypatch):
    from ee_agent.sandbox.runner import self_test

    monkeypatch.setenv("FAKE_API_KEY", "leak-me")
    probes = self_test()
    assert not probes["network"]["ok"]
    assert probes["secret"]["stdout"] == "no-secrets"
    assert not probes["write_outside"]["ok"]
    assert probes["write_inside"]["ok"]


def test_sandbox_refuses_unapproved_code():
    from ee_agent.errors import SandboxViolation
    from ee_agent.sandbox.runner import run

    with pytest.raises(SandboxViolation):
        run("print(1)", approve=False)


def test_sandbox_refuses_to_forward_a_secret():
    from ee_agent.errors import SandboxViolation
    from ee_agent.sandbox.runner import run

    with pytest.raises(SandboxViolation):
        run("print(1)", approve=True, extra_env={"POLYGON_API_KEY": "x"})


def test_review_banner_shows_the_code():
    from ee_agent.sandbox.runner import review_banner

    banner = review_banner("print('x')\nprint('y')", title="test")
    assert "REVIEW GATE" in banner and "has not run yet" in banner and "print('x')" in banner


# ================================================================= research
def test_research_index_records_every_run(spec, bars):
    from ee_agent.engine.truth import TruthEngine
    from ee_agent.research.index import read_index

    before = len(list(read_index()))
    TruthEngine(spec, monte_carlo_paths=50, synthetic_paths=0).analyze(bars.slice(0, 2000), pin=False)
    rows = list(read_index())
    assert len(rows) == before + 1
    last = rows[-1]
    assert last["spec_hash"] == spec.hash
    assert "cost_usd" in last and "metrics" in last


def test_overnight_reports_only_survivors(spec, bars):
    from ee_agent.research.autonomy import run_overnight

    report = run_overnight(spec, bars.slice(0, 4000), max_variants=2, monte_carlo_paths=50,
                           synthetic_paths=0, announce=False)
    assert report.n_variants >= 3
    assert all(v.verdict for v in report.survivors + report.rejected)
    assert report.corrected_sharpe is not None
    assert "variants of your" in report.morning_brief()


def test_variants_are_perturbations_not_new_strategies(spec):
    from ee_agent.research.autonomy import generate_variants

    for _description, variant in generate_variants(spec, max_variants=6):
        assert [r.id for r in variant.signals.entry] == [r.id for r in spec.signals.entry]
        assert variant.context[0].type == spec.context[0].type


def test_scanner_refuses_a_non_portable_spec(owner_spec):
    from ee_agent.research.autonomy import cross_asset_scan

    report = cross_asset_scan(owner_spec, symbols=["MNQ", "ES"])
    assert report.skipped.get("ALL")
    assert not report.rows


def test_hypothesis_queue_round_trips(tmp_path):
    from ee_agent.research.autonomy import HypothesisQueue

    queue = HypothesisQueue(path=tmp_path / "h.jsonl")
    queue.add("what if the stop were wider on high-volatility days?")
    assert len(queue.pending()) == 1
    queue.complete(queue.pending()[0].id, {"net_pnl": 100})
    assert not queue.pending()


def test_calendar_produces_blackouts(bars):
    from ee_agent.research.calendar import EventCalendar

    calendar = EventCalendar()
    mask = calendar.blackout_mask(bars.slice(0, 5000))
    assert mask.dtype == bool
    assert "cannot be" in calendar.coverage_note


def test_narration_says_what_it_is_waiting_on(spec, small_bars):
    from ee_agent.research.narration import SessionNarrator

    narrator = SessionNarrator(spec, sink=lambda _m: None)
    events = narrator.narrate_session(small_bars.slice(0, 400))
    assert events
    assert narrator.waiting_on(small_bars, 10)


def test_tearsheet_is_written(spec, bars, tmp_path):
    from ee_agent.engine.truth import TruthEngine
    from ee_agent.research.tearsheet import write_tearsheet

    report = TruthEngine(spec, monte_carlo_paths=50, synthetic_paths=0).analyze(bars.slice(0, 3000), pin=False)
    paths = write_tearsheet(report, out_dir=tmp_path)
    html = paths.html.read_text(encoding="utf-8")
    assert "The case against this result" in html
    assert html.index("The case against this result") < html.index("Equity curve")
    assert paths.text.exists()


# =================================================================== ledger
def test_signal_ledger_chain_verifies(tmp_path, spec):
    from ee_agent.ledger.signals import SignalLedger

    ledger = SignalLedger(tmp_path / "signals.jsonl")
    for i in range(4):
        ledger.record(spec_id=spec.id, spec_hash=spec.hash, plan_hash="p", symbol="MNQ",
                      timeframe="5m", side="long", bar_time=f"2026-01-0{i+1}T14:30:00Z", price=100 + i)
    assert ledger.verify().valid


def test_outcome_is_not_part_of_the_hash(tmp_path, spec):
    from ee_agent.ledger.signals import SignalLedger

    ledger = SignalLedger(tmp_path / "signals.jsonl")
    record = ledger.record(spec_id=spec.id, spec_hash=spec.hash, plan_hash="p", symbol="MNQ",
                           timeframe="5m", side="long", bar_time="2026-01-01T14:30:00Z", price=100)
    before = record.entry_hash
    ledger.attach_outcome(1, {"pnl": 500})
    assert ledger.entries()[0].entry_hash == before
    assert ledger.verify().valid
    proof = ledger.proof_for(1)
    assert "outcome" not in proof["record"] and proof["matches"]


def test_editing_history_breaks_the_chain(tmp_path, spec):
    import json

    from ee_agent.ledger.signals import SignalLedger

    path = tmp_path / "signals.jsonl"
    ledger = SignalLedger(path)
    for i in range(3):
        ledger.record(spec_id=spec.id, spec_hash=spec.hash, plan_hash="p", symbol="MNQ",
                      timeframe="5m", side="long", bar_time=f"2026-01-0{i+1}T14:30:00Z", price=100 + i)
    entries = ledger.entries()
    entries[1].price = 999.0
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry.to_dict()) + "\n")
    verification = ledger.verify()
    assert not verification.valid and verification.broken_at == 2


def test_deleting_an_entry_breaks_the_chain(tmp_path, spec):
    import json

    from ee_agent.ledger.signals import SignalLedger

    path = tmp_path / "signals.jsonl"
    ledger = SignalLedger(path)
    for i in range(4):
        ledger.record(spec_id=spec.id, spec_hash=spec.hash, plan_hash="p", symbol="MNQ",
                      timeframe="5m", side="short", bar_time=f"2026-01-0{i+1}T14:30:00Z", price=100 + i)
    entries = [e for e in ledger.entries() if e.seq != 2]
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry.to_dict()) + "\n")
    assert not ledger.verify().valid


def test_track_record_claims_only_what_it_can_prove(tmp_path, spec):
    from ee_agent.ledger.signals import SignalLedger

    ledger = SignalLedger(tmp_path / "signals.jsonl")
    for i in range(3):
        ledger.record(spec_id=spec.id, spec_hash=spec.hash, plan_hash="p", symbol="MNQ",
                      timeframe="5m", side="long", bar_time=f"2026-01-0{i+1}T14:30:00Z", price=100)
    ledger.attach_outcome(1, {"pnl": 100})
    track = ledger.track_record()
    assert track["signals_committed"] == 3 and track["outcomes_attached"] == 1
    assert track["open_or_unsettled"] == 2
    assert "before its outcome existed" in track["claim"]


# ================================================================== library
def test_library_is_offered_once_and_never_pushed(monkeypatch, tmp_path):
    import ee_agent.library.loader as loader

    monkeypatch.setenv("EE_HOME", str(tmp_path))
    assert loader.should_offer()
    text = loader.offer_text()
    assert "rather not build from scratch" in text
    assert "not mention this again" in text
    loader.mark_offered()
    assert not loader.should_offer()


def test_library_entries_are_full_specs():
    from ee_agent.library import loader

    entry = loader.get("sweep-return-v1")
    spec = entry.load_spec()
    assert spec.signals.entry and spec.risk.stop and spec.costs.commission_per_side > 0


def test_missing_originals_are_reported_not_skipped(bars):
    from ee_agent.library import loader

    delta = loader.signal_count_delta("sweep-return-v1", bars.slice(0, 1000))
    assert "note" in delta
