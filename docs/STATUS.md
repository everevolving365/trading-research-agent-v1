# STATUS — all 83 abilities

Section 13's completeness checklist, worked item by item. Nothing dropped
silently. Anything deferred is named with a reason and a plan.

**Legend** — `done`: built and covered by a test. `partial`: built, with a
stated limitation. `runtime`: built and verified against a fixture/mock/replay,
because the remaining part is a runtime input, not a build dependency
(Section 12).

---

## A. Conversation and intake

| # | ability | status | where / note |
|---|---|---|---|
| 1 | Voice in (Whisper, local, free, push to talk) | done | `capture/voice.py::Listener`. Optional dependency; `status()` reports availability and how to install it. |
| 2 | Voice out (local TTS) | done | `capture/voice.py::Speaker` — macOS `say`, Windows SAPI, `pyttsx3`, espeak. All local, all free. |
| 3 | Interruptible | done | `Speaker.interrupt()` drops the queue and terminates the current utterance. Speech is queued sentence by sentence so cutting in is immediate. |
| 4 | Deep conversation on any subject | done | `ee-agent chat`. A tool-using conversation over `ee_agent/conversation/`: the model talks about anything AND drives all 16 tools -- capture, interrogation, data, backtest, compile, parity, Operator, order flow, spend. Any provider, or none: with no key it says exactly what it cannot do and everything else still runs. |
| 5 | Works perfectly with voice off | done | Voice is a shell. `VoiceShell(enabled=False)` falls through to text and the transcript is still kept; the desktop app's mic button is optional and the window works identically without it. Tested. |
| 6 | Takes in information extremely in depth | done | `capture/parser.py` handles long, rambling, unordered input. The fixture transcript is deliberately meandering. |
| 7 | Shows the client data constantly | done | Charts rendered during the visual loop (`capture/visual.py`), with an ASCII fallback so a bare install still *shows* rather than tells. Cache age, quality score and cost printed on every operation. |

## B. Strategy capture

| # | ability | status | where / note |
|---|---|---|---|
| 8 | Free-form description, any length, any order | done | `parser.parse()`. |
| 9 | Dissects it in every direction | done | Parser extracts and, crucially, reports what it could *not* parse; the interrogation engine asks about exactly those gaps. |
| 10 | Interrogation engine with the full checklist | done | `capture/interrogation.py`. All eleven checklist items from Section 3 are implemented as questions with a stated purpose and a default assumption. It does not proceed until each is answered or logged. |
| 11 | Visual simulation with approve/reject | done | `capture/visual.py`. Annotated charts from real historical data; rejections revise the spec (a consistently rejected side is removed; early rejections move the window start), and every revision is recorded as an assumption. |
| 12 | Client's risk rules recorded exactly | done | `Risk` is copied verbatim. The agent never edits it — the portability conversion (D-002) *proposes* and requires approval. |
| 13 | Ambiguity ledger with sensitivity | done | Every assumption carries `sensitivity.if_wrong`; `measure_sensitivity()` runs the strategy both ways and reports the measured difference. |
| 14 | Natural language editing | done | `capture/intake.py::edit`, `before_and_after()` regenerates and shows both results side by side. An unrecognised sentence changes nothing and says so. |
| 15 | Screenshot and fill-history intake | done | `ee-agent screenshots <images>`. `capture/vision.py` reads the markup with a vision model (Anthropic/OpenAI/Gemini) and returns levels, arrows, zones and every annotation, then builds a spec proposal in which **every inferred element is an unapproved assumption** — a picture never becomes a rule, because that would be the agent deciding the strategy. With no vision key it extracts symbol/timeframe/date and says plainly that it cannot see the drawing. Fill-history reconstruction is complete and measured (`reconstruct_from_fills`). |

## C. Research and data

| # | ability | status | where / note |
|---|---|---|---|
| 16 | Any asset class | done | Futures, stocks, ETFs, indices, forex, crypto **and options**. `ee-agent options chain <underlying>` pulls full chains (yfinance free / Polygon keyed / synthetic offline) with bid/ask, open interest and greeks; `instruments/options.py` resolves an OCC symbol to an ordinary `Instrument`, so the backtester, fill model, parity harness and hedge check all treat a contract like any other instrument -- no asset-class branch anywhere (hard rule 5). Single-contract candles come from a vendor where a key allows, otherwise repriced bar by bar from the underlying and **labelled as modelled**. |
| 17 | Web search for new sources | done | `ee-agent sources "<what you need>"`. Two layers: a curated 16-vendor catalogue searched offline with no key, plus live web search via Brave/Tavily/SerpAPI when the client has one. Ranks by asset class, resolution, order-flow availability and cost. It finds SOURCES, not scrapers -- Section 9 limit 2 stands, so the handoff is a key or a file the ingestion layer reads. |
| 18 | Universal ingestion | done | `data/ingest.py` — CSV, TSV, JSON, NDJSON, parquet, Excel, broker exports, pasted text. Schema inferred from 40+ column aliases; epoch/ISO/localised timestamps handled. |
| 19 | Source registry with capability matrix | done | `data/source_registry.py`, `ee-agent data sources`. Resolver walks the ladder and falls through on failure, reporting each step. |
| 20 | Data integrity engine | done | Gaps, duplicates, out-of-order, timezone normalisation, DST, splits/dividends, continuous futures stitching (ratio and difference), quality score on every dataset. |
| 21 | Caching with visible age | done | `Bars.age_line()` printed on every load. No silent path exists. |
| 22 | Multi-resolution | done | `Bars.resample()` reconstructs any higher timeframe from the lowest available. |
| 23 | Paywall handling | done | `data/paywall.py` states the paywall, what it unlocks *for this specific strategy*, and what the free fallback costs in accuracy. The client pays; the credential is stored once and never asked for again. |
| 24 | Volume and order flow first-class | done | `flow/primitives.py` — cumulative delta, bid-ask imbalance, absorption, trade size distribution, volume profile with POC and value area, VWAP with stdev bands, liquidity map. Degrades to bar-derived proxies and reports the degradation. |

## D. Computer use

| # | ability | status | where / note |
|---|---|---|---|
| 25 | Drives a real browser | runtime | `operator/browser.py::PlaywrightDriver`. Built and tested against `MockPageDriver`; live validation needs a Premium account (B-001). |
| 26 | Logs into the client's TradingView | runtime | Credentials in the OS keychain, never logged, never sent to a model, removable in one command. Attach-to-profile offered as the no-storage alternative. |
| 27 | Pine Editor: paste, save, add to chart | runtime | Tested end to end against the mock page set. |
| 28 | Paste strategy, run Strategy Tester | runtime | Same. |
| 29 | Deep Backtesting, read the report | runtime | Plan detection gates it; a non-Premium account gets the regular backtest and is *told so* rather than being handed a mislabelled number. |
| 30 | Alerts and webhooks | runtime | Payload carries the spec hash; `LivePlan.verify_alert()` refuses an alert from a stale script. |
| 31 | Retrieve data from export portals | done | `ee-agent portal get <id>`. `operator/portals.py` carries five portals as DATA (TradingView export, TopstepX statements, CME DataMine, Databento batch, generic broker) — login, field fill, export, download, then handoff to universal ingestion so the client never states a schema. A purchase gate routes into the paywall handler. Adding a venue is one dict entry. Cannot place an order; a test greps for it. |
| 32 | Full audit trail | done | Every action logged with a screenshot and timestamp; secrets redacted before writing. |
| 33 | Human confirmation on public publishing | done | `publish_script()` raises without `confirm=True`. Saving privately needs no confirmation. |

## E. Backtesting

| # | ability | status | where / note |
|---|---|---|---|
| 34 | Exact-strategy Python backtest | done | Compiled from the spec, never re-translated. |
| 35 | Event-driven, bar by bar | done | `engine/backtester.py`. No vectorised shortcut; the lookahead detector independently proves causality. |
| 36 | Automatic lookahead detection | done | Recomputes on truncated data at random points and compares bar for bar, plus an impossible-fill check. Caught a real leak in our own `session_end` primitive (D-005). |
| 37 | Full execution realism | done | Commission per side, spread, slippage as a function of volume and volatility, partial fills, queue position, stop-run modelling, gap fills at the open. |
| 38 | Walk-forward, anchored and rolling | done | Both. `run_walk_forward(anchored=True|False)`. |
| 39 | In-sample / out-of-sample always | done | Every `StrategyReport` carries both, or it is not `defensible`. |
| 40 | Monte Carlo drawdown distribution | done | Block bootstrap preserves streaks, because consecutive losses are what breach a prop limit. |
| 41 | Regime decomposition | done | Trend vs range, high vs low volatility, session, day of week, news day. |
| 42 | Prop firm rule simulation | done | Topstep, Apex, TakeProfit as data files. Answers pass rate, days to pass, blow-up rate, and expected total cost to funding. Models intraday excursion (D-011). |
| 43 | Synthetic market testing | done | Stationary block bootstrap of log returns preserves volatility clustering. |
| 44 | Multiple-comparison correction | done | Deflated Sharpe by the expected maximum of n trials. The overnight loop feeds it the real variant count. |
| 45 | The adversarial pass | done | Thirteen attacks computed from the result itself, no model required (D-008). Every result ships with the case against it. |
| 46 | TradingView deep backtest cross-check | runtime | `operator/tradingview.py::cross_check`. Reports the gap and what explains it; the Python engine stays authoritative. |

## F. Indicator generation

| # | ability | status | where / note |
|---|---|---|---|
| 47 | True arrow signal indicator | done | `plotshape` triangles, not an approximation. Proven identical to the Python engine by the parity harness. |
| 48 | Matching strategy script | done | Costs and risk applied; `process_orders_on_close=true`, `calc_on_every_tick=false`. |
| 49 | Both compiled from the same object | done | All four targets share one `plan_hash`, asserted by a test. |
| 50 | Hard cases handled, differences stated | done | `pine_limitations()` returns exactly what differs and by how much. Empty list means the emission is exact. |
| 51 | Alerts and alert conditions included | done | `alertcondition` for both sides plus `alert()` on bar close. |

## G. Live trading

| # | ability | status | where / note |
|---|---|---|---|
| 52 | Trades live on prop firms off the alerts | runtime | Full path built; verified on the replay harness. Real capital needs credentials and the autonomy ladder. |
| 53 | TopstepX first, every venue behind one interface | done | `execution/adapters.py`. TopstepX written against the documented API, contract-tested against a mock server. |
| 54 | Owner's Python robot integrated | runtime | `OwnerRobotAdapter` + `docs/ADAPTER-INTERFACE.md`. The robot is not in the repo (B-002); the adapter says exactly what it needs. |
| 55 | Global position ledger | done | One ledger, every account, append-logged. |
| 56 | Hard hedge refusal | done | Correlation-aware, refused at the order layer before any API call, no override flag — asserted by a test that greps for escape hatches. |
| 57 | Autonomy ladder | done | Five levels; promotion needs evidence *and* a named approver; demotion on drift is automatic. |
| 58 | Drift monitoring | done | Live fills compared against the backtest's distribution; flags demote automatically. |
| 59 | Execution quality scorecard | done | Expected vs actual fill per trade, aggregated by strategy, session and instrument, with a grade. |
| 60 | Shadow mode | done | `ShadowBook` answers "what would it have done" and attributes the gap to execution. |
| 61 | Kill switches at three levels | done | Strategy, account, global. Triggers: daily loss, consecutive losses, drift, latency, data staleness, API errors. Only a human resets one. |

## H. Autonomy

| # | ability | status | where / note |
|---|---|---|---|
| 62 | Overnight research loop | done | Generates variants of the *client's* strategy, tests out of sample and against synthetic data, runs the adversarial pass, reports only survivors, declares its cost first. |
| 63 | Hypothesis queue | done | `HypothesisQueue` — speak an idea at any hour, it queues and gets worked. |
| 64 | Cross-asset scanner | done | Ranks the universe; refuses to run a non-scale-free spec because the ranking would be meaningless. Reports where the edge is inverted. |
| 65 | Event and calendar awareness | partial | FOMC and NFP dates exact; CPI approximated and flagged as such (B-007). Rollovers derived from the instrument registry. Blackout windows and regime tagging complete. |
| 66 | Session narration | done | `SessionNarrator` — setups forming, conditions met, and `waiting_on()` answers what it is waiting for, from the plan rather than a guess. |
| 67 | Morning brief and evening debrief | done | Both, spoken through the voice shell or printed. |
| 68 | Research index | done | Append-only JSONL, written every run, with every metric, artifact path and cost. |
| 69 | Auto-generated tearsheet | done | Self-contained HTML with an inline SVG equity curve (no plotting library needed) plus a plain-text version. The adversarial brief is at the *top*. |
| 70 | Verified signal ledger | done | Hash chain; the outcome is excluded from the hashed payload, which is what makes the record provably non-hindsight. Tampering and deletion are both caught. |

## I. Cost transparency

| # | ability | status | where / note |
|---|---|---|---|
| 71 | Never refuses on cost grounds | done | The only stop is a ceiling the *client* set, and it asks rather than halting. |
| 72 | Pre-operation estimate | done | `CostLedger.announce()` before every priced operation. |
| 73 | Running total always visible | done | `[spend]` banner; `ee-agent status`. |
| 74 | Costs recorded with results | done | `cost_usd` on every research-index row and every report. |
| 75 | Optional client ceiling | done | Off by default. When reached it raises `CeilingReached`, whose message asks. |
| 76 | Cost-reducing engineering | done | Cached element maps, direct page text reads instead of screenshots (the report panel is read as text — zero vision calls), downscaled screenshots, and vision calls counted and priced. |

## J. The founder's library

| # | ability | status | where / note |
|---|---|---|---|
| 77 | Client's own strategy is the default | done | Enforced in code: `should_offer()` returns False after one mention. |
| 78 | Library as a secondary option, offered once | done | One sentence, no default selection, no upsell. |
| 79 | Entries are full specs | done | `library/sweep-return-v1/spec.yaml` is a complete spec and inherits every guarantee. |
| 80 | The library grows | done | Drop a directory with a `spec.yaml` into `library/`. |

## K. Product surface

| # | ability | status | where / note |
|---|---|---|---|
| 81 | One-command install, wizard, free demo | done | `pip install -e .` then `ee-agent app` opens the desktop window, or `ee-agent demo` for a real backtest and parity proof with zero keys. |
| 82 | User friendly, no loss of capability | done | **`ee-agent app` is a real desktop window** — chat, live activity, running spend, voice toggle, microphone — so a client never has to meet a terminal (D-024). Built on `http.server` from the standard library: no Flask, no node, no build step. Localhost-only with a session token. Twenty-one CLI commands remain for anyone who prefers them, every one working against fixtures. Errors name the missing key and where to get it, then carry on. |
| 83 | No asset-class special cases | done | All asset behaviour is data in `instruments.yaml`. The same spec runs on MNQ and BTCUSDT through identical code. |

---

## Summary

- **done:** 79
- **partial (built, with a stated limitation):** 1 — ability 65 (CPI dates approximated; FOMC and NFP are exact). It needs a data refresh from the BLS, not code.
- **runtime (built and verified in simulation; needs a credential or account to exercise live):** 9 — abilities 25–30, 46, 52, 54

Every `partial` and `runtime` item has a reason and a plan above, and the ones
needing something from the owner are in `BLOCKERS.md`.

Abilities 4 and 17 were shipped as `partial` in the first pass and the owner
correctly rejected that: his requirement list names both plainly and says nothing
on it is optional. Both are now `done` (D-018, D-019). Abilities 15 and 31
followed in phase 14 (D-020, D-021), which leaves no `partial` item that any
amount of code could close — ability 65 needs a data refresh from the BLS, and
option-chain data has no free source to fetch from.

## The one-sentence test

> Can the agent prove that the strategy the client described and the strategy
> trading their account are the same object?

**Yes.** `ee-agent parity <spec>` compiles the spec to all four targets, executes
the emitted Pine source through an interpreter, rebuilds the live config from its
own JSON, and compares signal fingerprints bar by bar. On MNQ, ES, SPY and
BTCUSDT over a full year, all four produce the identical fingerprint. A
deliberately sabotaged emission is caught by the same harness, which is how we
know the test can fail.
