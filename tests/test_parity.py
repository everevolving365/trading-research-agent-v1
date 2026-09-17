"""The parity harness -- one of the two most thoroughly tested things here
(Section 2.4), because a silent failure costs real money.

If these pass, the one-sentence test passes: the strategy the client described
and the strategy trading their account are the same object, provably.
"""
from __future__ import annotations

import numpy as np
import pytest

from ee_agent.compile.to_pine import compile_to_pine_indicator, compile_to_pine_strategy
from ee_agent.compile.to_python import compile_to_python
from ee_agent.data.loader import load_bars
from ee_agent.parity import fingerprint as fp
from ee_agent.parity.harness import explain_divergence, run_parity
from ee_agent.parity.pine_sim import PineError, run_pine

#: Four asset classes in three exchange timezones. SPY and EURUSD are here
#: because they are NOT in the strategy's declared timezone -- which is how the
#: timezone bug in D-013 was caught.
INSTRUMENTS = [("MNQ", "5m"), ("ES", "5m"), ("SPY", "5m"), ("EURUSD", "5m"), ("BTCUSDT", "15m")]


def _bars(symbol, timeframe, limit=None):
    bars = load_bars(symbol, timeframe, fixtures_only=True, use_cache=False, sink=lambda _m: None)
    return bars.slice(0, limit) if limit else bars


# ------------------------------------------------------------ fingerprints
def test_fingerprint_is_order_independent_of_construction(spec, small_bars):
    signals = compile_to_python(spec).evaluate(small_bars)
    a = fp.from_arrays("a", small_bars, signals.long_entry, signals.short_entry)
    b = fp.from_arrays("b", small_bars, signals.long_entry, signals.short_entry)
    assert a.digest == b.digest


def test_fingerprint_changes_when_one_signal_moves(spec, small_bars):
    signals = compile_to_python(spec).evaluate(small_bars)
    base = fp.from_arrays("a", small_bars, signals.long_entry, signals.short_entry)
    moved = signals.long_entry.copy()
    index = int(np.flatnonzero(moved)[0])
    moved[index] = False
    moved[index + 1] = True
    other = fp.from_arrays("b", small_bars, moved, signals.short_entry)
    assert base.digest != other.digest
    divergences = fp.compare([base, other], bars=small_bars)
    assert divergences


def test_compare_locates_the_divergence_to_the_bar(spec, small_bars):
    signals = compile_to_python(spec).evaluate(small_bars)
    base = fp.from_arrays("python", small_bars, signals.long_entry, signals.short_entry)
    broken = signals.long_entry.copy()
    index = int(np.flatnonzero(broken)[2])
    broken[index] = False
    other = fp.from_arrays("pine_indicator", small_bars, broken, signals.short_entry)
    divergences = fp.compare([base, other], bars=small_bars)
    assert divergences[0].bar_index == index
    assert "python" in divergences[0].present_in
    assert "pine_indicator" in divergences[0].absent_from


# --------------------------------------------------------- the real thing
@pytest.mark.parametrize("symbol,timeframe", INSTRUMENTS)
def test_all_four_targets_agree(spec, symbol, timeframe):
    result = run_parity(spec, _bars(symbol, timeframe))
    assert not result.errors, result.errors
    assert len(result.fingerprints) == 4
    assert result.agreed, "\n".join(d.explain() for d in result.divergences[:5])
    assert len({f.digest for f in result.fingerprints}) == 1


def test_parity_holds_on_the_owners_own_spec(owner_spec):
    result = run_parity(owner_spec, _bars("MNQ", "5m"))
    assert result.agreed, "\n".join(d.explain() for d in result.divergences[:5])


def test_parity_holds_over_a_full_year(spec):
    bars = _bars("MNQ", "5m")
    span_days = (bars.last_bar_time - bars.first_bar_time).days
    assert span_days > 250, f"fixture covers only {span_days} days"
    assert run_parity(spec, bars).agreed


def test_parity_detects_a_real_divergence(spec, small_bars):
    """Sabotage the emitted Pine and prove the harness notices."""
    artifact = compile_to_pine_indicator(spec)
    sabotaged = artifact.source.replace("longSignal  = ", "longSignal  = false and ")
    series = run_pine(sabotaged, small_bars, ["longSignal", "shortSignal"], mintick=0.25)
    python = compile_to_python(spec).evaluate(small_bars)
    a = fp.from_arrays("python", small_bars, python.long_entry, python.short_entry)
    b = fp.from_arrays(
        "pine_indicator", small_bars,
        np.asarray(series["longSignal"], dtype=bool), np.asarray(series["shortSignal"], dtype=bool),
    )
    assert a.digest != b.digest
    assert fp.compare([a, b], bars=small_bars)


def test_explain_divergence_shows_every_condition(spec, small_bars):
    divergence = fp.Divergence(ts=small_bars.df["ts"].iloc[100].isoformat(), side="long",
                               present_in=["python"], absent_from=["live"], bar_index=100)
    text = explain_divergence(spec, small_bars, divergence)
    assert "python long=" in text
    assert "context:" in text


# ------------------------------------------------------- the interpreter
def test_interpreter_refuses_what_it_does_not_implement(small_bars):
    with pytest.raises(PineError):
        run_pine("x = ta.percentrank(close, 20)\nlongSignal = x > 50\n", small_bars, ["longSignal"])


def test_interpreter_handles_var_persistence(small_bars):
    source = """//@version=5
indicator("t")
var int counter = 0
counter := counter + 1
longSignal = counter > 5
shortSignal = false
"""
    series = run_pine(source, small_bars.slice(0, 20), ["longSignal"])
    assert series["longSignal"][:5].sum() == 0
    assert bool(series["longSignal"][-1])


def test_interpreter_matches_python_moving_averages(small_bars):
    from ee_agent.spec.primitives import _ema, _sma

    source = """//@version=5
indicator("t")
fast = ta.sma(close, 10)
slow = ta.ema(close, 20)
longSignal = fast > slow
shortSignal = false
"""
    series = run_pine(source, small_bars.slice(0, 500), ["longSignal"])
    bars = small_bars.slice(0, 500)
    expected = _sma(bars.close, 10) > _ema(bars.close, 20)
    valid = ~np.isnan(_sma(bars.close, 10)) & ~np.isnan(_ema(bars.close, 20))
    got = np.asarray(series["longSignal"], dtype=bool)
    assert (got[valid] == expected[valid]).all()


def test_interpreter_session_windows_respect_timezone(small_bars):
    source = """//@version=5
indicator("t")
inSess = not na(time(timeframe.period, "0930-1500:1234567", "America/Chicago"))
longSignal = inSess
shortSignal = false
"""
    series = run_pine(source, small_bars, ["longSignal"])
    got = np.asarray(series["longSignal"], dtype=bool)
    expected = (small_bars.minute_of_day >= 570) & (small_bars.minute_of_day < 900)
    assert (got == expected).all()


def test_interpreter_time_period_open_changes_once_a_day(small_bars):
    source = """//@version=5
indicator("t")
newDay = ta.change(time("D")) != 0
longSignal = newDay
shortSignal = false
"""
    series = run_pine(source, small_bars, ["longSignal"])
    got = np.asarray(series["longSignal"], dtype=bool)
    n_days = len(set(small_bars.local_date.tolist()))
    assert abs(int(got.sum()) - (n_days - 1)) <= 1, "time(\"D\") did not change exactly once per day"


def test_strategy_script_signals_match_the_indicator(spec, small_bars):
    indicator = run_pine(
        compile_to_pine_indicator(spec).source, small_bars, ["longSignal", "shortSignal"], mintick=0.25
    )
    strategy = run_pine(
        compile_to_pine_strategy(spec).source, small_bars, ["longSignal", "shortSignal"], mintick=0.25
    )
    assert (
        np.asarray(indicator["longSignal"], dtype=bool) == np.asarray(strategy["longSignal"], dtype=bool)
    ).all()
    assert (
        np.asarray(indicator["shortSignal"], dtype=bool) == np.asarray(strategy["shortSignal"], dtype=bool)
    ).all()
