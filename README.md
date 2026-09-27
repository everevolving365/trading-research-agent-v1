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
$ ee-agent parity nash-breaker-block-v2 --fixtures --symbols MNQ

[parity] nash-breaker-block-v2 on MNQ 5m over 16,365 bars
    python           90a3867a6c86     515 signals  {'long': 258, 'short': 257}
    pine_indicator   90a3867a6c86     515 signals  {'long': 258, 'short': 257}
    pine_strategy    90a3867a6c86     515 signals  {'long': 258, 'short': 257}
    live             90a3867a6c86     515 signals  {'long': 258, 'short': 257}
    original         fdce94431ebd      74 signals  {'long': 48, 'short': 26}  <- owner's script, most recent 2,500 bars
    python           fdce94431ebd      74 signals  {'long': 48, 'short': 26}  <- Python engine, the same bars
    AGREED -- all 4 targets produced the identical fingerprint 90a3867a6c86.
    AGREED -- the owner's own script (NASH-Breaker-Block-v2-ENTRY-SIGNALS.pine, unmodified)
              matches the Python engine signal for signal over the most recent 2,500 bars.
```

That is not four re-runs of one code path. The Python engine evaluates the
compiled plan; a Pine interpreter parses and executes the **emitted Pine source
text** bar by bar; the live config is rebuilt from its own JSON by the
execution layer. Four independent paths, one fingerprint.

And for a strategy ported from a script there is a fifth: **the script itself**.
NASH Breaker Block v2 is the owner's own TradingView indicator. Its original
source runs unmodified through the same interpreter and matches the port signal
for signal. The port is not a lookalike; it is provably the script.

## Install

It installs like any other program: an icon on your Desktop that opens the
agent in its own window.

**Windows** -- download the ZIP (green **Code** button, **Download ZIP**), open
the folder and double-click **`Install.bat`**. Or paste one line into
PowerShell:

```powershell
irm https://raw.githubusercontent.com/everevolving365/trading-research-agent-v1/main/install/windows/bootstrap.ps1 | iex
```

**Mac** -- double-click **`Install.command`**. **Linux** -- `bash install.sh`.

**Or ask Claude:** "install https://github.com/everevolving365/trading-research-agent-v1
on my computer". This works in the Claude desktop app or Claude Code, where
Claude can run commands; `CLAUDE.md` tells it exactly what to do. A plain
browser chat can't install software, so use the steps above there.

The installer finds or installs Python, builds a private environment in your
user folder, puts **EverEvolving Trading Agent** on your Desktop and in the
Start menu (Applications on a Mac), and opens it. It never asks for a password
or a key. Step by step, with what to click: [INSTALL.md](INSTALL.md).

The first thing to try in the app is the demo: a complete backtest and a parity
proof on data saved inside the app. **No API key, no network, no cost.** That is
deliberate -- see the zero-cost floor below.

### For developers

```bash
git clone https://github.com/everevolving365/trading-research-agent-v1.git
cd trading-research-agent-v1
pip install -e .
ee-agent demo
```

## Talk to it

Double-click the icon. The window has a chat, a microphone button, a live view
of what the agent is doing and what it has cost. It needs no terminal at all,
and closing the window closes the app.

Prefer a terminal? Everything is there too:

```bash
ee-agent app                  # the same window, started from a terminal
ee-agent chat                 # the same conversation, in the shell
ee-agent chat --voice         # out loud
ee-agent capture              # straight to the structured intake
```

`chat` is a tool-using agent, not a chat window. Ask it to pull a year of MNQ and
backtest your opening-range idea and it loads the data and runs the truth engine,
then reads you the case against the result. It cannot place an order -- order
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
ee-agent app                                # the desktop app -- start here
ee-agent chat                               # the same conversation in a terminal
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

On the committed fixture data -- synthetic prices, so this shows the machinery,
not how NASH trades real MNQ:

```
  nash-breaker-block-v2 on MNQ 5m  [full]
  Net P&L                3,868.74     Trades               249
  Max drawdown           6,628.00     Win rate          35.3%
[walk-forward] rolling, 5 window(s); 1 of 5 profitable out of sample
[monte carlo] 400 block paths: final P&L 5-95% band [-4,761, -1,032]; 0% of paths profitable
[lookahead] CLEAN -- 24 truncation point(s) re-evaluated, every signal identical with the future removed.
[combine] Topstep 50K Combine: 0% of 1,000 simulated runs pass, 86% blow it, 14% neither.
[adversarial] the case against this result:
    [SERIOUS] window dependence: Only 1 of 5 out-of-sample windows are profitable.
    [SERIOUS] path dependence: Only 0% of resampled paths finish profitable.
    [SERIOUS] profit concentration: The top 12 trade(s) are 202% of the net result.
    VERDICT: 4 serious findings. This does not survive the adversarial pass.
```

A positive net P&L and a verdict that it does not survive: the number alone
would have looked like an edge. It tells you when your strategy does not work. That is the product.

## The founder's library

One strategy ships with the agent, offered once and never pushed -- your own
strategy is always the default:

**NASH Breaker Block v2** (`library/nash-breaker-block-v2/`): a 5-minute close
through the latest confirmed swing high or low, taken only when a 15-minute fair
value gap sits behind the move. Ported rule for rule from the owner's published
TradingView script, which is kept byte for byte in `original/` and proven
identical by parity. The script draws entries only, so its stop, target and
trading hours are logged as assumptions waiting for the owner's approval; until
then the agent backtests it and will not trade it live.

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
| `ee_agent/ui/` | the desktop app: local server and the window itself |
| `ee_agent/conversation/` | the tool-using chat agent, model clients, the 20 tools |
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
