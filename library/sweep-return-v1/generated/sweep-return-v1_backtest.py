"""GENERATED -- do not edit.

Compiled from strategy spec: sweep-return-v1
Spec hash: sha256:fdafb21392e95fb5fa804fa7c824c6a400df9f452ae7440d789367bd1635e6f3
Plan hash: sha256:006b5f7297036988d61e6ec6ea8ad46aaf3ba30f159a291a11d4a912c57e65df

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
id: sweep-return-v1
name: EE Sweep Return v1
author: owner
created: '2026-09-13'
source_transcript: library/sweep-return-v1/how-i-trade-it.md
universe:
  instruments:
  - MNQ
  - NQ
  - ES
  - BTCUSDT
  session: rth
  timezone: America/Chicago
context:
- id: opening_range
  type: session_range
  window:
    start: 08:30
    end: 09:30
signals:
  entry:
  - id: short
    timeframe: 5m
    side: short
    all_of:
    - type: range_break
      reference: opening_range
      side: high
      confirm: wick
    - type: return_inside
      reference: opening_range
      confirm: close
  - id: long
    timeframe: 5m
    side: long
    all_of:
    - type: range_break
      reference: opening_range
      side: low
      confirm: wick
    - type: return_inside
      reference: opening_range
      confirm: close
  invalidation:
  - type: session_end
    reference: signal_window
filters:
  time_windows:
  - start: 09:30
    end: '15:00'
    tz: America/Chicago
  max_trades_per_day: 2
risk:
  stop:
    type: points
    value: 25.0
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
  session_close_behavior: flatten
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
- id: a1
  question: Close fully inside the range, or just past the broken level?
  resolution: just past the broken level
  approved_by: owner
  alternatives:
  - close fully inside the range
  sensitivity: {}
- id: a2
  question: Does a break of the high and a return inside on the SAME bar count?
  resolution: yes, same-bar break and return is a valid signal
  approved_by: owner
  alternatives:
  - require the return on a later bar
  sensitivity: {}
- id: a3
  question: Both directions, or short-only?
  resolution: both -- a break of the high that returns inside is a short, a break of the low that returns
    inside is a long
  approved_by: owner
  alternatives:
  - short only
  sensitivity: {}
notes: 'Owner''s ruling, authoritative (Section 11): the original indicator and strategy .pine files are

  the same strategy. The only intended difference is that the strategy script waits for a candle

  close. This spec is built from the indicator''s logic as the reference behaviour, with bar-close

  confirmation applied to entries. The two original files are retained in original/ as a

  regression baseline; the signal-count delta between generated and original is reported in

  docs/STATUS.md for information only.

  '
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
    print(json.dumps({"spec": "sweep-return-v1", "spec_hash": "sha256:fdafb21392e95fb5fa804fa7c824c6a400df9f452ae7440d789367bd1635e6f3", "metrics": out["metrics"]}, indent=2))
