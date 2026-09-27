# NASH Breaker Block v2: how it trades

> Read straight from the script (`original/NASH-Breaker-Block-v2-ENTRY-SIGNALS.pine`, published
> by the owner on TradingView). It describes what the code does. It is not the owner's own
> write-up, which is still to come. Where the owner's intent could differ from the code, the
> spec logs it as an assumption instead of guessing.

## The idea in one breath

Wait for price to **break structure**: a 5-minute candle closes through the most recent swing
high (for a long) or swing low (for a short). Only take the break if there is **fuel** behind
it, meaning a recent 15-minute fair value gap sitting on the side the move came from.

## The entry, rule by rule

1. **Swing points.** A swing high is a 5-minute high with 3 lower highs on its right and no
   higher high among the 3 bars on its left. It only counts once those 3 right-hand bars have
   printed. If two highs are equal, the later one is the swing (TradingView's rule). Swing lows
   work the same way in mirror image.
2. **The level.** The newest confirmed swing high is the long level and the newest swing low is
   the short level. A new swing replaces the old one.
3. **The break.** A long fires when a 5-minute candle **closes** above the long level. A short
   fires when one closes below the short level.
4. **One shot per level.** The first close through a level uses it up, whether or not the
   signal qualified. The same level cannot fire twice. A new swing brings a new level.
5. **The fuel.** A 15-minute fair value gap is a three-candle gap on the 15-minute chart:
   bullish when the low of the newest candle is above the high of the candle two back, bearish
   in mirror image. For a long, the break needs a **bullish** gap that:
   - was created in the last **60** five-minute bars, and before the breaking candle;
   - has its top **at or below** the broken level;
   - has its top **no more than 120 points** below the level.

   For a short it is a bearish gap whose bottom sits at or above the level, within 120 points.
   A gap price has already traded back into still counts, because that is the script's default.
6. **When a 15-minute gap exists.** The 15-minute values only count once that 15-minute candle
   has closed. On a 5-minute chart that is the third 5-minute candle of the quarter hour. No
   gap is ever seen early.

## Switches in the script that are off by default

| Script input | Default | In the spec |
|---|---|---|
| ALSO require EMA side (15-minute 21 EMA) | off | `htf_ema_side` (add it to switch it on) |
| ALSO require an open-level break (5pm / 11pm / 8:30 CT opens) | off | `session_open_levels` + `open_level_break` |
| FVG must match direction | on | `match_direction: true` |
| FVG must still be unmitigated | off | `must_be_unmitigated: false` |
| BOS break detection | Close | `confirm: close` |
| Structure pivot length | 3 | `pivot_len: 3` |

Every one of these has been checked against the original script with the same switch flipped,
and the signals match bar for bar.

## What the script does not say (and the spec does not assume)

The script draws entries only. It has no stop, no target, no time filter, no daily limit and no
rule for the session close. The spec fills those from the owner's TopstepX bracket (about $60
risk and $100 profit on one MNQ contract, so 30 and 50 points). Each one is marked
**pending** until the owner confirms it, and a pending assumption blocks live trading.

## Proof that the port is the script

`ee-agent parity library/nash-breaker-block-v2/spec.yaml --fixtures` runs five things over the
same bars: the Python engine, the generated indicator, the generated strategy, the live config,
and **the owner's original script, unmodified**. They must agree signal for signal. If they
disagree on any bar, the report shows exactly where.
