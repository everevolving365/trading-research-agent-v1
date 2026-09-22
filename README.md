# EverEvolving Trading Agent

A voice-native, deeply autonomous trading agent that is also a computer-using
agent. You describe your strategy and your risk rules out loud. It researches,
fetches data for any asset from anywhere, backtests in Python, builds the
TradingView indicator, logs into your TradingView account and runs the deep
backtest itself, and trades the strategy live on prop firms.

**You own the strategy. You own the risk.** The agent does not invent
strategies, does not set risk, and does not tell anyone what to trade.

## The guarantee that defines the product

> The strategy you described and the strategy trading your account are provably
> the same object, and the agent can show you the proof.

```
$ ee-agent parity library/sweep-return-v1/spec.yaml --fixtures

[parity] sweep-return-v1 on MNQ 5m over 17,061 bars
    python           37bcb3993135     223 signals  {'short': 114, 'long': 109}
    pine_indicator   37bcb3993135     223 signals  {'short': 114, 'long': 109}
    pine_strategy    37bcb3993135     223 signals  {'short': 114, 'long': 109}
    live             37bcb3993135     223 signals  {'short': 114, 'long': 109}
    AGREED -- all 4 targets produced the identical fingerprint 37bcb3993135.
```

That is not four re-runs of one code path. The Python engine evaluates the
compiled plan; a Pine interpreter parses and executes the **emitted Pine source
text** bar by bar; the live config is rebuilt from its own JSON by the execution
layer. Four independent paths, one fingerprint.

## Install

```bash
git clone https://github.com/everevolving365/trading-research-agent-v1.git
cd trading-research-agent-v1
pip install -e .
ee-agent demo
```

`ee-agent demo` runs a complete backtest and a parity proof on committed fixture
data. **No API key, no network, no cost.** That is deliberate — see the
zero-cost floor below.

Then talk to it, or describe your strategy directly:

```bash
ee-agent chat                 # open-ended conversation; it drives the whole agent
ee-agent chat --voice         # same thing, out loud
ee-agent capture              # straight to the structured intake
```

`chat` is a tool-using agent, not a chat window. Ask it to pull a year of MNQ and
backtest your opening-range idea and it loads the data and runs the truth engine,
then reads you the case against the result. It cannot place an order — order
placement lives behind the position ledger and a conversational model does not
get to reach it.

## Bring your own key

The product ships with nobody's credentials. You supply your own model key, your
own data keys and your own TradingView subscription; the author carries zero
per-user cost. `ee-agent wizard` walks you through obtaining each one, and
`ee-agent secrets list` shows what each unlocks.

Credentials go to your OS keychain (encrypted-file fallback), are never logged,
are never sent to any model, and `ee-agent secrets delete NAME` removes one in a
single command.

### The zero-cost floor

With **no keys at all**, the agent still: runs the full Python backtest engine,
compiles specs to all four targets, runs walk-forward, Monte Carlo, lookahead
detection and the parity proof, ingests any file you already have, pulls keyless
crypto data, and runs local voice. You reach a real backtest before spending
anything.

## The three laws

**Law One — never report a number you cannot defend.** Costs and slippage in
every backtest. In-sample and out-of-sample always. A Monte Carlo band on every
result. Lookahead detection on every run. Cache age printed on every load. A
data quality score on every dataset. A single-window result literally prints
*"SINGLE WINDOW -- not a defensible number on its own"*.

**Law Two — one strategy, one object, provably.** The spec is the single source
of truth. Python, Pine indicator, Pine strategy and live config are all compiled
from it, and the parity harness proves they agree signal for signal.

**Law Three — nothing untrusted runs unwatched.** Generated code is displayed
before it executes and runs in a sandbox with no network, no API keys, one
writable directory and a hard timeout. The position ledger refuses hedged orders
at the order layer. The autonomy ladder gates live capital. Kill switches exist
at strategy, account and global level.

## What it does

```bash
ee-agent chat                               # talk to it about anything; it runs every tool it has
ee-agent capture                            # describe your strategy; it interrogates you
ee-agent sources "<what data you need>"     # find where to get data for any asset
ee-agent screenshots <images>               # read your marked-up charts and infer the rules
ee-agent portal get <id>                    # pull data from an export portal or statement
ee-agent options chain SPY --delta 0.25     # option chains, greeks, liquidity, strike selection
ee-agent compile <spec>                     # -> Python, Pine indicator, Pine strategy, live config
ee-agent analyze <spec> --tearsheet         # the truth engine, plus the case against the result
ee-agent parity <spec>                      # prove all four targets are the same strategy
ee-agent operator <spec>                    # drive TradingView: paste, save, deep backtest, alerts
ee-agent replay <spec> --sessions 5         # recorded history through the LIVE path
ee-agent overnight <spec>                   # variants tested overnight, survivors only
ee-agent scan <spec>                        # where the edge is strongest, marginal, or inverted
ee-agent ledger verify                      # prove the track record was not written with hindsight
ee-agent status                             # keys, positions, kill switches, spend
ee-agent verify                             # every acceptance check for every phase
```

### A result looks like this

```
  sweep-return-v1 on MNQ 5m  [full]
  Net P&L              -6,216.96     Trades               329
  Max drawdown          6,295.37     Win rate          27.7%
[walk-forward] rolling, 5 windows; 0 of 5 profitable out of sample
[monte carlo] 2,000 block paths: drawdown median 6,393, 95th 8,514
[lookahead] CLEAN -- 24 truncation points re-evaluated, every signal identical
[combine] Topstep 50K: 0% of 1,000 simulated runs pass, 73% blow it
[adversarial] the case against this result:
    [SERIOUS] window dependence: Only 0 of 5 out-of-sample windows are profitable.
    [SERIOUS] path dependence: Only 0% of resampled paths finish profitable.
    VERDICT: 2 serious findings. This does not survive the adversarial pass.
```

It tells you when your strategy does not work. That is the product.

## Architecture

```
  VOICE + CONVERSATION      whisper in, TTS out, interruptible, narration
        |
  CAPTURE                   interrogation engine, visual confirmation, ambiguity ledger
        |
  >>> THE STRATEGY SPEC <<< single source of truth
        |
  SUBSTRATE                 instrument registry | data resolver | order flow | integrity
        |
  COMPILERS                 Python backtest | Pine indicator | Pine strategy | live config
        |
  PARITY PROOF              signal fingerprint comparison across all four
        |
  EXECUTION                 broker adapters | position ledger | autonomy ladder | drift monitor
        |
  MOAT                      research index | verified signal ledger | tearsheets

  OPERATOR (cross-cutting)  drives real software as a human would. Reads and configures only.
                            NEVER places orders -- enforced by a test that greps the module.
  COST NOTIFIER             estimates, informs, proceeds. Never blocks.
```

| module | what lives there |
|---|---|
| `ee_agent/spec/` | schema, validator, versioning, diffing, the primitive registry |
| `ee_agent/conversation/` | the tool-using chat agent, model clients, the 16 tools |
| `ee_agent/capture/` | voice, interrogation, visual confirmation, intake, NL editing |
| `ee_agent/instruments/` | tick size, tick value, sessions, holidays, rollover, correlation |
| `ee_agent/data/` | source registry, resolver, discovery, integrity engine, ingestion, cache, paywall |
| `ee_agent/flow/` | cumulative delta, absorption, volume profile, VWAP bands, liquidity map |
| `ee_agent/compile/` | `to_python`, `to_pine_indicator`, `to_pine_strategy`, `to_live` |
| `ee_agent/engine/` | backtester, fills, costs, walk-forward, Monte Carlo, lookahead, adversarial |
| `ee_agent/prop/` | one rule pack per firm, and the combine simulator |
| `ee_agent/parity/` | the Pine interpreter, fingerprinting, the harness |
| `ee_agent/sandbox/` | isolated execution of generated code |
| `ee_agent/operator/` | browser control, credential vault, audit trail |
| `ee_agent/execution/` | adapters, position ledger, autonomy, drift, kill switches, replay |
| `ee_agent/research/` | research index, hypothesis queue, overnight loop, scanner, calendar |
| `ee_agent/ledger/` | the verified signal ledger |
| `ee_agent/cost/` | estimation, notification, running total |

## Cost transparency

The agent never refuses an operation on cost grounds — it is not a gatekeeper.
Before anything with meaningful cost it tells you the estimate and what it
covers, then proceeds. A running total is always visible, every cost is written
into the research index next to the result it produced, and you may optionally
set your own ceiling. If you set one and it is reached, the agent tells you and
**asks** — it does not silently stop.

## Honest limits

1. **TradingView has no API for running backtests and never has.** The Operator
   closes this by driving the browser, but that is automation: slower, breaks
   when the interface changes, and Deep Backtesting needs Premium. Deep results
   appear only in the Strategy Tester report panel; the chart's trades come from
   the regular backtest and will not match — so the Operator **refuses** to read
   the chart's trade list rather than return a wrong number.
2. **Scraping arbitrary sites for minute data will not hold.** The source
   registry, the fallback ladder and universal ingestion deliver the same
   capability and stay standing.
3. **Order flow does not exist for most assets and is paid for the rest.** The
   flow layer degrades to bar-derived proxies and *says so on every call*.
4. **Prop firm rules do not apply to spot crypto or equities.** The rule-pack
   layer is optional per instrument, driven by the registry.
5. **Portal and TradingView selectors will break.** Every one of them is a dict
   entry (`PORTALS`, `SELECTORS`) rather than code, precisely because a layout
   change should be a one-line fix and not a debugging session.
6. **Some strategies cannot be expressed exactly in Pine.** Where that happens
   the agent states precisely what differs and by how much
   (`pine_limitations()`), rather than shipping a silent approximation.

## Development

```bash
pip install -r requirements-dev.txt
make verify        # every acceptance check for every phase, zero credentials
make test          # the pytest suite
```

`BUILD-LOG.md` is the resume file, `DECISIONS.md` records every decision made
autonomously, `BLOCKERS.md` lists what is still needed from the owner, and
`docs/STATUS.md` reports the state of all 83 abilities.

v1 (the Gemini-backed research CLI) is preserved under `legacy/v1/`.

## Licence

MIT. Not financial advice: this software executes the strategy and risk rules
its operator supplies. Trading futures and other leveraged instruments involves
substantial risk of loss.
