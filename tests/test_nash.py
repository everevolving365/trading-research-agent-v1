"""NASH Breaker Block v2: the port, the owner's original script, and the Pine
constructs that script needed from the interpreter."""
from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from ee_agent.compile.to_python import compile_to_python
from ee_agent.data.bars import Bars
from ee_agent.data.htf import htf_index_map, htf_view, pine_timeframe_minutes
from ee_agent.data.synthetic import GenSpec, generate
from ee_agent.errors import PrimitiveError
from ee_agent.parity import fingerprint as fp
from ee_agent.parity.harness import run_parity
from ee_agent.parity.original import load_original, original_for
from ee_agent.parity.pine_sim import PineError, run_pine
from ee_agent.spec import primitives as prim
from ee_agent.spec.model import StrategySpec
from ee_agent.spec.validator import validate

REPO = Path(__file__).resolve().parent.parent
NASH = REPO / "library/nash-breaker-block-v2"


def _raw() -> dict:
    return yaml.safe_load((NASH / "spec.yaml").read_text(encoding="utf-8"))


def _bars_from(highs, lows=None, closes=None, start="2026-03-02 14:30", freq="5min", tf="5m") -> Bars:
    highs = [float(h) for h in highs]
    lows = [float(x) for x in (lows or [h - 1 for h in highs])]
    closes = [float(x) for x in (closes or [h - 0.5 for h in highs])]
    ts = pd.date_range(start, periods=len(highs), freq=freq, tz="UTC")
    df = pd.DataFrame({"ts": ts, "open": closes, "high": highs, "low": lows, "close": closes, "volume": 1.0})
    return Bars(symbol="MNQ", timeframe=tf, df=df)


def _original_signals(bars, inputs=None):
    script = original_for("nash-breaker-block-v2")
    out = run_pine(script.source, bars, [script.long, script.short], inputs=inputs)
    return fp.from_arrays("original", bars, np.asarray(out[script.long], bool), np.asarray(out[script.short], bool))


def _python_signals(spec, bars):
    sig = compile_to_python(spec).evaluate(bars)
    return fp.from_arrays("python", bars, sig.long_entry, sig.short_entry)


# ------------------------------------------------------------ structure
def test_equal_highs_make_the_later_bar_the_pivot():
    bars = _bars_from([10, 11, 12, 15, 15, 13, 12, 11, 10, 9])
    level, _ = prim.structure_state(prim.Runtime(bars), "high", 3, "close")
    confirmed = np.flatnonzero(~np.isnan(level))
    assert confirmed[0] == 7 and level[7] == 15.0  # bar 4 + 3 bars to confirm


def test_an_equal_high_on_the_right_vetoes_the_pivot():
    # bar 3 = 15 has an equal high to its right (bar 5), so bar 3 is not a pivot;
    # bar 5 has an equal high on its LEFT, which does not stop it
    bars = _bars_from([10, 11, 12, 15, 14, 15, 13, 12, 11, 10])
    level, _ = prim.structure_state(prim.Runtime(bars), "high", 3, "close")
    confirmed = np.flatnonzero(~np.isnan(level))
    assert confirmed[0] == 8


def test_a_level_fires_once_then_is_burned():
    highs = [10, 11, 12, 20, 13, 12, 11, 21, 22, 23, 24]
    closes = [9, 10, 11, 12, 12, 11, 10, 20.5, 21, 22, 23]
    bars = _bars_from(highs, closes=closes)
    level, broke = prim.structure_state(prim.Runtime(bars), "high", 3, "close")
    assert level[6] == 20.0
    assert broke.tolist().count(True) == 1 and broke[7]  # later closes above 20 do not re-fire


def test_wick_break_uses_the_high():
    highs = [10, 11, 12, 20, 13, 12, 11, 21, 12]
    closes = [9, 10, 11, 12, 12, 11, 10, 19, 11]
    bars = _bars_from(highs, closes=closes)
    _, by_close = prim.structure_state(prim.Runtime(bars), "high", 3, "close")
    _, by_wick = prim.structure_state(prim.Runtime(bars), "high", 3, "wick")
    assert not by_close.any() and by_wick[7]


# ------------------------------------------------------ higher timeframe
def test_a_15m_value_arrives_on_the_closing_5m_bar():
    bars = _bars_from(list(range(100, 112)), start="2026-03-02 15:00")  # 15:00 .. 15:55 UTC
    htf, kmap = htf_view(bars, "15")
    assert len(htf) == 4
    assert kmap.tolist() == [-1, -1, 0, 0, 0, 1, 1, 1, 2, 2, 2, 3]


def test_a_missing_closing_bar_delays_the_value_to_the_next_bar():
    bars = _bars_from(list(range(100, 112)), start="2026-03-02 15:00")
    gap = pd.concat([bars.slice(0, 2).df, bars.slice(3, 12).df], ignore_index=True)
    gapped = Bars(symbol="MNQ", timeframe="5m", df=gap)
    htf, kmap = htf_view(gapped, "15")
    # 15:10 is missing, so the 15:00 bar is first visible on 15:15 (which closes at 15:20)
    assert kmap.tolist()[:3] == [-1, -1, 0]


def test_timeframe_rules_are_explicit():
    assert pine_timeframe_minutes("15") == 15 and pine_timeframe_minutes("1h") == 60
    with pytest.raises(PrimitiveError):
        pine_timeframe_minutes("D")
    bars = _bars_from(list(range(10)))
    with pytest.raises(PrimitiveError):
        htf_view(bars, "1")  # below the chart timeframe
    htf, kmap = htf_view(bars, "5")
    assert kmap.tolist() == list(range(10))


def test_fair_value_gaps_are_recorded_with_the_scripts_geometry():
    # three 15m bars: H[0]=100 < L[2]=105 -> bullish gap, top = 105, bottom = 100
    rows_h = [98, 99, 100, 101, 102, 103, 110, 111, 112]
    rows_l = [95, 96, 97, 98, 99, 100, 105, 106, 107]
    bars = _bars_from(rows_h, lows=rows_l, start="2026-03-02 15:00")
    table = prim.fvg_table(prim.Runtime(bars), "15")
    assert table["top"] == [105.0] and table["bot"] == [100.0] and table["dir"] == [1]
    assert table["bar"] == [8]  # the third 15m bar closes on the ninth 5m bar


# ------------------------------------------------------ the original script
def test_the_original_is_byte_checked():
    script = original_for("nash-breaker-block-v2")
    assert script is not None and script.long == "bosLong" and script.short == "bosShort"
    assert script.sha256.startswith("2e26a1cbd799")


def test_a_modified_original_is_refused(tmp_path):
    entry = tmp_path / "nash"
    (entry / "original").mkdir(parents=True)
    for name in ("manifest.yaml", "NASH-Breaker-Block-v2-ENTRY-SIGNALS.pine"):
        (entry / "original" / name).write_bytes((NASH / "original" / name).read_bytes())
    pine = entry / "original" / "NASH-Breaker-Block-v2-ENTRY-SIGNALS.pine"
    pine.write_bytes(pine.read_bytes().replace(b"input.int(3,", b"input.int(4,"))
    with pytest.raises(ValueError, match="checksum"):
        load_original(entry)


@pytest.mark.parametrize("seed", [3, 11])
def test_the_port_matches_the_original_on_round_the_clock_data(seed):
    bars = generate(GenSpec("MNQ", "5m", days=4, all_hours=True, seed=seed, daily_vol=0.02))
    spec = StrategySpec.from_dict(_raw())
    ours, theirs = _python_signals(spec, bars), _original_signals(bars)
    assert len(ours.signals) > 10
    assert ours.digest == theirs.digest, fp.compare([ours, theirs], bars=bars)[:3]


def test_the_ema_switch_matches_the_original():
    bars = generate(GenSpec("MNQ", "5m", days=4, all_hours=True, seed=5, daily_vol=0.02))
    raw = copy.deepcopy(_raw())
    for rule in raw["signals"]["entry"]:
        rule["all_of"].append({"type": "htf_ema_side", "timeframe": "15", "length": 21})
    ours = _python_signals(StrategySpec.from_dict(raw), bars)
    theirs = _original_signals(bars, {"ALSO require EMA side (off for now)": True})
    assert ours.digest == theirs.digest


def test_five_targets_agree_on_the_fixture():
    from ee_agent.data.loader import load_bars

    bars = load_bars("MNQ", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None).slice(0, 1500)
    result = run_parity(StrategySpec.load(NASH / "spec.yaml"), bars)
    assert result.agreed, result.report()
    assert [f.target for f in result.fingerprints][-1] == "original"
    assert "including the owner's own script" in result.report()


def test_a_long_run_compares_the_original_over_its_window():
    from ee_agent.data.loader import load_bars

    bars = load_bars("MNQ", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None).slice(0, 3000)
    result = run_parity(StrategySpec.load(NASH / "spec.yaml"), bars)
    assert result.original is not None and result.original.windowed
    assert result.original.n_bars == 2500 and result.original.agreed
    assert result.agreed


def test_a_disagreeing_original_fails_parity():
    from ee_agent.data.loader import load_bars

    bars = load_bars("MNQ", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None).slice(0, 1200)
    script = original_for("nash-breaker-block-v2")
    script.inputs = {"Structure Pivot Length (bars each side)": 5}
    result = run_parity(StrategySpec.load(NASH / "spec.yaml"), bars, original=script)
    assert not result.agreed
    assert result.divergences


# ------------------------------------------------------------- compiling
def test_the_committed_artifacts_are_current():
    from ee_agent.compile.to_live import compile_to_live
    from ee_agent.compile.to_pine import compile_to_pine_indicator, compile_to_pine_strategy

    spec = StrategySpec.load(NASH / "spec.yaml")
    gen = NASH / "generated"

    def same(path, text):
        return path.read_bytes().replace(b"\r\n", b"\n") == text.encode("utf-8").replace(b"\r\n", b"\n")

    assert same(gen / "nash-breaker-block-v2.pine", compile_to_pine_indicator(spec).source)
    assert same(gen / "nash-breaker-block-v2-STRATEGY.pine", compile_to_pine_strategy(spec).source)
    assert same(gen / "nash-breaker-block-v2-live.json", compile_to_live(spec).to_json())


def test_two_rules_share_one_structure_declaration():
    from ee_agent.compile.to_pine import compile_to_pine_indicator

    raw = copy.deepcopy(_raw())
    extra = copy.deepcopy(raw["signals"]["entry"][0])
    extra["id"] = "long2"
    del extra["all_of"][0]["pivot_len"]  # the default -- the same level
    raw["signals"]["entry"].append(extra)
    source = compile_to_pine_indicator(StrategySpec.from_dict(raw)).source
    assert source.count("var float bos_hi_p3c_lvl") == 1


def test_validator_requires_a_break_beside_the_fuel():
    raw = copy.deepcopy(_raw())
    raw["signals"]["entry"][0]["all_of"].pop(0)
    report = validate(StrategySpec.from_dict(raw))
    assert any(f.code == "SIG010" for f in report.errors)


def test_validator_checks_the_context_type():
    raw = copy.deepcopy(_raw())
    raw["context"].append({"id": "orng", "type": "session_range", "window": {"start": "08:30", "end": "09:30"}})
    raw["signals"]["entry"][0]["all_of"][1]["reference"] = "orng"
    report = validate(StrategySpec.from_dict(raw))
    assert any(f.code == "PRIM006" for f in report.errors)


def test_exits_stay_pending_and_block_live():
    spec = StrategySpec.load(NASH / "spec.yaml")
    pending = {a.id for a in spec.unresolved_assumptions()}
    assert {"stop_placement", "target_logic"} <= pending
    assert not validate(spec).live_ready


def test_the_fuel_distance_is_part_of_portability(owner_spec, bars):
    from ee_agent.spec.portability import is_portable, portability_note, to_atr_units

    assert "fvg_fuel.max_distance" in (portability_note(owner_spec) or "")
    converted, rows = to_atr_units(owner_spec, bars)
    assert any(r.field.endswith("fvg_fuel.max_distance") for r in rows)
    assert is_portable(converted)
    assert converted.signals.entry[0].all_of[1].params["max_distance"]["type"] == "atr"


# ------------------------------------------------ interpreter constructs
def _run(source, n=40, collect=("x",), **kw):
    bars = _bars_from([100 + (i % 7) for i in range(n)])
    return run_pine(source, bars, list(collect), **kw), bars


def test_user_functions_have_local_scope_and_return_the_last_value():
    src = """//@version=6
f(a) =>
    b = a * 2
    if b > 10
        b
    else
        -b
x = f(4) + f(6)
"""
    out, _ = _run(src, n=3)
    assert out["x"][0] == -8 + 12


def test_for_loops_count_down_and_break():
    src = """//@version=6
var arr = array.new_float()
array.push(arr, bar_index)
total = 0.0
for i = array.size(arr) - 1 to 0
    if array.get(arr, i) < bar_index - 2
        break
    total += array.get(arr, i)
x = total
"""
    out, _ = _run(src, n=6)
    assert out["x"].tolist() == [0, 1, 3, 6, 9, 12]


def test_tuples_comma_declarations_and_else_if():
    src = """//@version=6
a = 1, b = 2
[c, d] = [a + 10, b + 20]
x = 0
if c > 100
    x := 1
else if d == 22
    x := 2
else
    x := 3
"""
    out, _ = _run(src, n=2)
    assert out["x"].tolist() == [2, 2]


def test_inputs_default_and_override_by_title():
    src = """//@version=6
len = input.int(3, "Length", minval=1)
x = len
"""
    out, _ = _run(src, n=1)
    assert out["x"][0] == 3
    out, _ = _run(src, n=1, inputs={"Length": 9})
    assert out["x"][0] == 9


def test_v5_evaluates_both_sides_of_and_v6_does_not():
    body = """
var calls = array.new_float()
touch() =>
    array.push(calls, 1)
    true
x = false and touch()
y = array.size(calls)
"""
    out5, _ = _run("//@version=5" + body, n=3, collect=("y",))
    out6, _ = _run("//@version=6" + body, n=3, collect=("y",))
    assert out5["y"].tolist() == [1, 2, 3]
    assert out6["y"].tolist() == [0, 0, 0]


def test_request_security_refuses_lookahead_on():
    src = """//@version=5
x = request.security(syminfo.tickerid, "15", close, lookahead=barmerge.lookahead_on)
"""
    with pytest.raises(PineError, match="future data"):
        _run(src)


def test_request_security_delivers_closed_bars_only():
    src = """//@version=5
x = request.security(syminfo.tickerid, "15", close)
"""
    bars = _bars_from(list(range(100, 112)), start="2026-03-02 15:00")
    out = run_pine(src, bars, ["x"])
    x = out["x"]
    assert np.isnan(x[0]) and np.isnan(x[1])
    assert x[2] == bars.close[2] and x[3] == bars.close[2] and x[5] == bars.close[5]


def test_lower_timeframe_requests_are_recorded_as_approximations():
    src = """//@version=6
arr = request.security_lower_tf(syminfo.tickerid, "1", open)
x = array.size(arr)
"""
    notes: list[str] = []
    bars = _bars_from(list(range(10)))
    out = run_pine(src, bars, ["x"], notes=notes)
    assert out["x"][0] == 1 and notes and "one element" in notes[0]


def test_unknown_constructs_still_fail_loudly():
    with pytest.raises(PineError):
        _run("//@version=6\nwhile true\n    x = 1\n")
    with pytest.raises(PineError):
        _run("//@version=6\nx = ta.percentrank(close, 20)\n")


# ------------------------------------------------------ described in words
def test_describing_nash_in_words_gives_the_same_signals():
    """A client who describes this strategy in plain English gets the same
    entries as the owner's script -- and is asked about what they left out."""
    from ee_agent.capture.parser import parse
    from ee_agent.data.loader import load_bars

    text = (REPO / "tests/fixtures/transcripts/nash-breaker.txt").read_text(encoding="utf-8")
    parsed = parse(text, strategy_id="nash-captured")
    assert parsed.found["pattern"] == "break of structure with a fair value gap behind it"
    assert parsed.found["pivot_len"] == "3" and parsed.found["gap_distance"] == "120 points"
    assert "which hours this trades" in parsed.missing
    assert validate(parsed.spec).ok
    bars = load_bars("MNQ", "5m", fixtures_only=True, use_cache=False, sink=lambda _m: None).slice(0, 2000)
    captured = _python_signals(parsed.spec, bars)
    library = _python_signals(StrategySpec.load(NASH / "spec.yaml"), bars)
    assert captured.digest == library.digest


def test_an_unstated_gap_rule_is_asked_not_assumed():
    from ee_agent.capture.parser import parse

    parsed = parse("I trade MNQ on the 5 minute chart. I buy a break of structure with a fair value gap behind it.")
    assert "how far from the broken level the gap may sit" in parsed.missing
    assert "how old the gap may be" in parsed.missing
    assert "how many bars on each side make a swing high or low" in parsed.missing
    assert "which timeframe the fair value gaps come from" in parsed.missing
