"""GENERATED -- do not edit.

Compiled from strategy spec: nash-breaker-block-v2
Spec hash: sha256:f9c1c85613cd5ee9a6f477841d7edb9dbc1c8912367fca17b1ac227ddc0463b4
Plan hash: sha256:411ad65c4cba1a4597f6eb61198bd8602d13b3bc17de48a9a0f7130863895f3b

This file is deterministic output of ee_agent.compile.to_python. It contains no
model-written code. Regenerating it from the same spec produces the same bytes.
"""
from __future__ import annotations

import json
import sys

from ee_agent.spec.model import StrategySpec
from ee_agent.compile.to_python import compile_to_python
from ee_agent.engine.backtester import Backtester
from ee_agent.data.loader import load_bars

SPEC_YAML = r"""
spec_version: 1
id: nash-breaker-block-v2
name: NASH Breaker Block v2
author: owner
created: '2026-09-26'
source_transcript: library/nash-breaker-block-v2/how-i-trade-it.md
universe:
  instruments:
  - MNQ
  - NQ
  - ES
  - MES
  session: eth
  timezone: America/Chicago
context:
- id: fvg15
  type: htf_fvg
  timeframe: '15'
signals:
  entry:
  - id: long
    timeframe: 5m
    side: long
    all_of:
    - type: structure_break
      side: high
      pivot_len: 3
      confirm: close
    - type: fvg_fuel
      reference: fvg15
      max_distance:
        type: points
        value: 120
      max_age: 60
      match_direction: true
      must_be_unmitigated: false
  - id: short
    timeframe: 5m
    side: short
    all_of:
    - type: structure_break
      side: low
      pivot_len: 3
      confirm: close
    - type: fvg_fuel
      reference: fvg15
      max_distance:
        type: points
        value: 120
      max_age: 60
      match_direction: true
      must_be_unmitigated: false
filters: {}
risk:
  stop:
    type: points
    value: 30.0
  target:
    type: points
    value: 50.0
  size:
    method: fixed_contracts
    value: 1.0
  max_concurrent_positions: 1
  scale_in: false
execution:
  entry_order: market_on_close_of_signal_bar
  session_close_behavior: hold
  on_overlapping_signal: ignore_new
  gap_behavior: fill_at_open
  bar_close_confirmation: true
costs:
  commission_per_side:
    unit: currency
    value: 2.0
  slippage:
    unit: ticks
    value: 1.0
  spread:
    unit: ticks
    value: 1.0
  slippage_model: volume_volatility
  exchange_fees_per_side:
    unit: currency
    value: 0.0
assumptions:
- id: entry_trigger
  question: What fires the entry?
  resolution: 'The script''s own BOS + FVG engine with its default inputs, exactly: a 5-minute close through
    the latest confirmed 3-bar pivot high (long) or low (short), once per level, with a bullish (bearish)
    15-minute fair value gap created in the last 60 bars whose top (bottom) sits at or below (above) the
    broken level and within 120 points of it.'
  approved_by: owner-script
  sensitivity: {}
- id: invalidation
  question: What tells you the setup is dead?
  resolution: 'The script''s own rule: a structure level can fire once. The first close through it burns
    it, whether or not a gap qualified, and only a newer confirmed pivot brings a new level.'
  approved_by: owner-script
  sensitivity: {}
- id: stop_placement
  question: Where does the stop go? The indicator draws entries only -- it has no stop.
  resolution: 30 points from entry, read from the TopstepX Auto OCO bracket on the owner's account (risk
    ~$60 on 1 MNQ at $2/point). NOT confirmed for this strategy.
  approved_by: pending
  alternatives:
  - 15 points (the same $60 bracket if it applied to the 2-contract position the old bot traded)
  - a stop beyond the fair value gap or the pivot that was broken
  sensitivity: {}
- id: target_logic
  question: Where does the target go? The indicator has no exit.
  resolution: 50 points from entry, read from the same Auto OCO bracket (profit ~$100 on 1 MNQ). NOT confirmed
    for this strategy.
  approved_by: pending
  alternatives:
  - 25 points (the $100 bracket on a 2-contract position)
  - the opposite structure level, or a fixed R multiple of the stop
  sensitivity: {}
- id: session_filter
  question: Which hours does it trade? The script has no time filter.
  resolution: every bar the chart shows -- no filter, as the script is written
  approved_by: pending
  alternatives:
  - New York session only (08:30-15:00 CT)
  sensitivity: {}
- id: max_trades_per_day
  question: How many signals a day will you take?
  resolution: no cap -- the script does not cap signals
  approved_by: pending
  alternatives:
  - one or two a day, as the Topstep daily loss limit would suggest
  sensitivity: {}
- id: session_close_behavior
  question: What happens to an open position at the session close?
  resolution: held until the bracket closes it (the script never flattens)
  approved_by: pending
  alternatives:
  - flatten at 15:00 CT
  sensitivity: {}
notes: "The owner's own open-source TradingView script, NASH Breaker Block v2 ENTRY SIGNALS\n(https://www.tradingview.com/script/bpH84GWN-NASH-Breaker-Block-v2-ENTRY-SIGNALS/),\
  \ kept verbatim\nin original/. The entry rules are the script's BOS + FVG engine with its default inputs:\
  \ nothing\nis inferred, and the original runs as a fifth parity target -- the generated indicator, the\n\
  strategy script, the Python engine, the live config and the owner's own script must agree\nsignal for\
  \ signal.\n\nThe script draws entries only. Everything about exits (stop, target, session close, daily\
  \ cap)\nis logged above as a PENDING assumption: it came from the owner's TopstepX bracket settings,\n\
  not from this script, and the agent does not set risk (hard rule 1). Until the owner approves\nor replaces\
  \ them the spec backtests, and the validator blocks it from live capital.\n\nThe script's two optional\
  \ add-ons are OFF by default and so are off here. To switch one on, add\nto both rules' all_of:\n  \"\
  ALSO require EMA side\"         -> {type: htf_ema_side, timeframe: '15', length: 21}\n  \"ALSO require\
  \ an open-level break\" -> a session_open_levels context (17:00, 23:00, 08:30 CT)\n                \
  \                    and {type: open_level_break, reference: <its id>, confirm: wick}\nBoth are parity-tested\
  \ against the original script with the matching input switched on.\n"
"""


def main(symbol: str = "MNQ", timeframe: str = "5m") -> dict:
    spec = StrategySpec.from_yaml(SPEC_YAML)
    bars = load_bars(symbol, timeframe, lookback_days=365)
    print(bars.age_line())
    result = Backtester(spec).run(bars)
    print(result.report())
    return result.to_dict()


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "MNQ"
    out = main(symbol)
    print(json.dumps({"spec": "nash-breaker-block-v2", "spec_hash": "sha256:f9c1c85613cd5ee9a6f477841d7edb9dbc1c8912367fca17b1ac227ddc0463b4", "metrics": out["metrics"]}, indent=2))
