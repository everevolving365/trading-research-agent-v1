# DECISIONS

Append-only. Every decision made on the owner's behalf, per Section 2.1. He
reads this at the end, not during.

---

## D-001 — v1 moved to `legacy/v1/`, v2 lives in `ee_agent/`
Date: 2026-09-16
Context: the repo held the v1 CLI agent at the top level. v2 is a different
architecture, not an evolution of those files.
Options: (a) delete v1; (b) leave it at the top level alongside v2; (c) move it
to `legacy/v1/`.
Chosen: (c).
Reasoning: v1 still runs and the owner may want it; leaving it at the top level
would make the repo root ambiguous about what the product is.
Reversible: yes — `git mv legacy/v1/* .`

---

## D-002 — `points` and `ticks` are not treated as portable units
Date: 2026-09-16
Context: Section 4 requires dimensionless parameters, and the example spec uses
`points: 25`. But a point is a price unit: 25 points is 0.12% of MNQ and 0.04%
of Bitcoin. Running the example spec unedited on BTCUSDT produced a 0% win rate
because the stop was inside the noise.
Options: (a) silently rescale per instrument; (b) accept it and let the Phase 2
criterion pass on a technicality; (c) treat only `percent`, `atr`, `stdev` and
`bps` as scale-free, warn loudly on the others, and offer a measured conversion.
Chosen: (c). The validator raises `PORT001` — blocking when the universe spans
more than one asset class — and `ee_agent/spec/portability.py` measures the
equivalent ATR multiples and shows them for approval.
Reasoning: (a) is forbidden by hard rule 1: silently changing the client's stop
is the agent deciding their risk. (b) would mean shipping a false claim. (c)
tells the truth and gives the client the choice.
Reversible: yes — the check is one validator rule.
Note: `library/sweep-return-v1/spec.yaml` keeps the owner's 25/50 points exactly
as stated. `tests/fixtures/specs/sweep-return-atr.yaml` is the converted,
portable version used for cross-asset work.

---

## D-003 — `return_inside` is an edge, not a state
Date: 2026-09-16
Context: read literally, "price is back inside the range" is true on every bar
after the return, so the indicator would plot an arrow on all of them.
Options: (a) state semantics; (b) edge semantics (fires on the bar price
re-enters); (c) a parameter.
Chosen: (c) defaulting to (b): `mode: cross` fires when the bar closes back
inside having either swept the level with its wick (the same-bar case the owner
explicitly allows) or closed outside on the previous bar. `mode: state` is
available for specs that genuinely mean the state reading.
Reasoning: "it comes back inside" describes an event. State semantics produced
2,740 long signals on one year of MNQ where edge semantics produce 505.
Reversible: yes — set `mode: state` in the spec.

---

## D-004 — Parity is proven by interpreting the emitted Pine, not by re-running the plan
Date: 2026-09-16
Context: Law Two demands the four targets be provably the same object. The easy
implementation — evaluate the shared IR twice and compare — compares a thing to
itself and proves nothing.
Options: (a) compare IR to IR; (b) shell out to TradingView (no API exists);
(c) write a Pine interpreter for the subset we emit.
Chosen: (c). `ee_agent/parity/pine_sim.py` tokenises, parses and executes the
emitted Pine source bar by bar with Pine's series semantics. The live config is
evaluated on its own separate path from JSON.
Reasoning: it is the only option that actually tests the artifact the client
pastes into TradingView. The interpreter fails loudly on any construct it does
not implement rather than skipping it, so it cannot pass by ignoring code.
Reversible: yes, but it should not be — this is the proof the product is sold on.
Honest limit: it proves the emitted script computes the same signals under a
faithful implementation of the constructs used. Only TradingView can prove
TradingView agrees, which is what the Operator's deep backtest cross-check is
for. The Python engine remains authoritative.

---

## D-005 — `session_end` does not fire on the last bar of the dataset
Date: 2026-09-16
Context: the first implementation marked the final array element as a session
end. Our own lookahead detector caught it: the signal changed when the series
was truncated, because "the data ran out" was being treated as a market event.
Options: (a) exempt the last bar from the detector; (b) stop marking it.
Chosen: (b). The backtester closes anything still open at the end of the data
separately and says so in the result notes.
Reasoning: (a) would have been suppressing a true positive from the one detector
whose job is to catch exactly this.
Reversible: yes, but it would reintroduce the leak.

---

## D-006 — Autonomy caps gate entries only, never exits
Date: 2026-09-16
Context: the level-4 daily-loss cap was refusing exit orders, which left a
position open after the limit was hit.
Options: (a) leave it; (b) exempt orders that reduce exposure.
Chosen: (b). `OrderRouter._reduces_exposure()` is checked before the autonomy
gate; kill switches and the hedge check still apply to every order.
Reasoning: a daily loss limit exists to stop losses growing. Blocking the exit
achieves the exact opposite and traps the client in the position.
Reversible: yes.

---

## D-007 — Pine fragment deduplication is per fragment, not per line
Date: 2026-09-16
Context: two primitives emitting the same `if opening_range_newday` header had
the second one dropped by line-level dedup, orphaning its indented body and
producing Pine that would not compile.
Options: (a) unique-ify every identifier; (b) dedup whole fragments by primitive
and parameters.
Chosen: (b). Contexts emit once; conditions always emit.
Reasoning: a generated script that does not compile is worse than a verbose one.
Reversible: yes.

---

## D-008 — The adversarial pass computes its findings without a model
Date: 2026-09-16
Context: Section 3 ability 45 describes "a second reasoning pass". Making that a
model call would mean the zero-cost floor ships without an adversarial pass, and
would make the findings non-deterministic.
Options: (a) model-only; (b) computed findings, model optionally used for prose.
Chosen: (b). Eleven attacks are computed from the result itself — sample size,
lookahead, out-of-sample degradation, window dependence, regime concentration,
cost sensitivity, path dependence, understated drawdown, synthetic
indistinguishability, selection, parameter fragility, data quality, profit
concentration. `narrate()` hands the same evidence to a model when one is
configured.
Reasoning: a finding that only appears when a client has paid for an API key is
not a safety feature. Determinism also means the verdict is reproducible.
Reversible: yes.

---

## D-009 — The capture parser is deterministic, with the model as an optional extractor
Date: 2026-09-16
Context: Phase 4 needs to turn speech into a spec, and the obvious route is a
model call.
Options: (a) model-only; (b) deterministic extraction that reports what it could
not parse, with the interrogation engine asking about exactly those gaps.
Chosen: (b).
Reasoning: bring-your-own-key means a client may have no model at all, and the
zero-cost floor requires reaching a real backtest without one. More importantly,
a model that quietly invents a missing rule breaks hard rule 1. The parser
extracts what it recognises and says what it did not; the interrogation engine
does the rest.
Reversible: yes — a model extractor can be layered on top without changing the
interface.

---

## D-010 — Three prop firms shipped, not one
Date: 2026-09-16
Context: Phase 3 asks for rule packs "starting with Topstep".
Options: (a) Topstep only; (b) Topstep plus two more.
Chosen: (b) — Topstep, Apex, TakeProfit.
Reasoning: one rule pack does not prove the layer is data-driven. Three with
materially different rules (intraday-peak versus end-of-day trailing drawdown,
daily loss limit present versus absent) proves it, and exercises both trailing
implementations in the combine simulator.
Reversible: trivially — they are YAML files.

---

## D-011 — The combine simulator models intraday excursion
Date: 2026-09-16
Context: bootstrapping daily closing P&L and checking it against a daily loss
limit is optimistic: a day that closes at -400 may have been -640 at its worst,
and that is what actually breaches the limit.
Options: (a) close-only; (b) model the intraday swing.
Chosen: (b), with `intraday_swing_factor` defaulting to 1.6 and exposed as a
parameter.
Reasoning: an optimistic pass rate is the single most expensive number to get
wrong here — it is what the client pays evaluation fees against.
Reversible: yes — set the factor to 1.0.

---

## D-012 — MIT licence
Date: 2026-09-16
Context: Phase 0 says "add a LICENSE" without naming one.
Options: MIT, Apache-2.0, proprietary.
Chosen: MIT, with a plain-language "not financial advice" notice appended.
Reasoning: the product is sold as a download to clients who supply their own
keys; MIT is the least friction. The trading disclaimer is not optional given
what the software does.
Reversible: yes, until third parties rely on it.

---

## D-013 — Clock comparisons use the strategy's timezone; day boundaries use the exchange's
Date: 2026-09-17
Context: the parity harness diverged on SPY. The Python primitives were reading
the *bars'* local time, so "09:30 to 15:00 Central" was silently evaluated as
09:30–15:00 New York on a New York instrument, while the emitted Pine correctly
used Central. It agreed on MNQ and ES only because those instruments happen to
be in the strategy's timezone.
Options: (a) make Pine follow the bars' timezone; (b) make Python follow the
declared timezone.
Chosen: (b), with a split — `Runtime.calendar(tz)` gives minute-of-day in the
declared timezone, `Runtime.session_days()` gives the exchange-local date. Clock
comparisons (windows, ranges) use the former; anything that resets daily uses
the latter, because that is what Pine's `time("D")` does.
Reasoning: the client said "Central", so Central is what it means on every
instrument. Had this shipped, a strategy validated on MNQ would have traded a
one-hour-shifted window on SPY with no warning — the exact per-asset drift hard
rule 5 exists to prevent.
Reversible: yes, but it would reintroduce the bug.
Found by: the parity harness, on an instrument nobody had thought to check.

---

## D-014 — Timestamp arithmetic never divides a raw int64 view
Date: 2026-09-17
Context: the integrity engine failed to detect a deliberately injected 12-bar
hole. It computed gaps as `ts.astype("int64") / 60e9`, assuming nanoseconds;
pandas 3 stores this data as `datetime64[us]`, so every gap was scaled by
1/1000 and nothing ever exceeded the threshold.
Options: (a) pin pandas; (b) normalise the unit at every conversion site.
Chosen: (b). Gap detection uses `.diff().dt.total_seconds()`, which is
resolution-independent. `Bars.content_hash` normalises to nanoseconds before
hashing so a pinned artifact hashes the same on every machine.
Reasoning: (a) would leave a silent correctness bug waiting for an upgrade, and
the failure mode is the worst kind — the check runs, reports clean, and finds
nothing.
Reversible: no reason to.

---

## D-015 — Recorded sources rank last in the live ladder
Date: 2026-09-17
Context: the resolver sorted by availability, then reliability, then cost. A
fixture has reliability 1.0 and zero cost, so it outranked every real source —
the agent would have served recorded bars instead of the market and reported
them as a normal load.
Options: (a) special-case fixtures in the loader; (b) mark recorded sources as
last-resort in the registry and sort them after every live source.
Chosen: (b). `fixtures_only=True` still forces them, which is how the tests and
the demo run offline.
Reasoning: the failure was silent, and "stale data reported as live" is the kind
of thing Law One exists to prevent.
Reversible: yes.
Found by: a test asserting the ladder prefers a real source.

---

## D-016 — A missing session is a gap; a truncated session is reported too
Date: 2026-09-17
Context: gap detection forgave any jump that crossed a date change, on the
grounds that markets close overnight. That also forgave an entire absent trading
day, and a session that simply stopped halfway through scored a clean 1.00.
Options: (a) leave it; (b) count skipped trading days using the instrument's own
calendar, and separately compare each session's bar count against the typical
one.
Chosen: (b). The first and last sessions are exempt from the short-session check
because a dataset almost always starts and ends mid-session, and flagging that
would cry wolf on every clean file.
Reasoning: a quality score that says 1.00 on a dataset with a day missing is
worse than no quality score, because it is trusted.
Reversible: yes.

---

## D-017 — Rule pack notes are block scalars
Date: 2026-09-17
Context: `- End-of-day trailing drawdown: the threshold moves...` in a YAML list
is parsed as a mapping key, not a string. `takeprofit.yaml` failed to load at
all, and nothing noticed until a test loaded every pack rather than just Topstep.
Options: (a) remove the colons; (b) write every note as a `>-` block scalar.
Chosen: (b), and the test now loads *every* firm rather than the default one.
Reasoning: rule packs are data the owner will edit. They must tolerate ordinary
prose, and the test must cover all of them or the next one added breaks silently.
Reversible: no reason to.

---

## D-018 — The conversation is a tool-using agent, not a chat window
Date: 2026-09-18
Context: ability 4 ("deep conversation on any subject") shipped as `partial` in
the first pass -- all the domain logic worked, but there was no way to simply
talk to the agent. The owner rejected that, correctly: his list names it and
says nothing on it is optional.
Options: (a) a plain chat wrapper that answers questions about trading; (b) a
conversation with tool access to the entire system.
Chosen: (b). Sixteen tools cover capture, interrogation, data, discovery,
backtest, compile, parity, Operator, order flow, library, research index and
spend. The model does the work rather than describing it.
Reasoning: a chat window that says "you could run a backtest" is worse than no
chat window -- the client already has the CLI. The value is that "backtest my
idea on a year of MNQ" actually loads the data and runs the truth engine.
Guard rails, enforced in code not prompt: no conversational tool can reach the
order layer (a test greps for it), and no tool invents a strategy or a risk rule
-- `answer_question` refuses a non-answer like "whatever you think is best"
rather than filling the gap itself.
Reversible: yes -- it is an additive package plus one CLI command.

---

## D-019 — Source discovery finds vendors, not scrapers
Date: 2026-09-18
Context: ability 17 ("searches the web to find data") shipped as `partial`
because Section 9 limit 2 says scraping arbitrary sites will not hold. That
reasoning was sound about the MECHANISM and wrong as a reason not to ship the
CAPABILITY.
Options: (a) leave it unbuilt and cite Section 9; (b) scrape price data from
whatever the search turns up; (c) search for data SOURCES and hand off to a key
or a file.
Chosen: (c), in two layers. A curated catalogue of 16 real vendors is searched
offline with no key and no network; live web search runs through the client's
own Brave, Tavily or SerpAPI key when they have one. Results are ranked by asset
class, resolution, order-flow availability and cost, and a scraper-access source
is ranked DOWN.
Reasoning: (b) is the thing Section 9 warns about -- a strategy resting on a
scraper stops working without warning. (a) leaves a stated requirement unbuilt.
(c) delivers what the owner asked for through the mechanism that survives
contact with reality, and universal ingestion means any file found this way is
readable without the client describing its schema.
Reversible: yes.
Honest limit: the catalogue's prices and coverage will drift. Each entry carries
a vendor URL and the file says to verify before relying on a number.

---

## D-020 — A screenshot produces questions, never rules
Date: 2026-09-21
Context: ability 15 asks the agent to read marked-up screenshots of past trades
and "infer the rules". Inferring a rule from a picture and then trading it is
the agent deciding the client's strategy, which hard rule 1 forbids outright.
Options: (a) read the image and write a spec; (b) read the image and write a
spec proposal in which everything inferred is an unapproved assumption.
Chosen: (b). `spec_from_screenshots()` emits one assumption per observation
class -- levels, arrow directions, annotations, timeframe, risk -- all with
`approved_by: pending`, which the validator treats as blocking. The first
assumption is always `screenshot:entry_rule`, whose text says outright that a
picture shows WHERE the client entered and not WHY.
Reasoning: a vision model can see that a line was drawn at 20125.25. It cannot
know whether that is a session range, a prior high or a round number the client
happens to like, and guessing puts a rule in the client's strategy that they
never stated.
Reversible: yes, but doing so would break hard rule 1.
Honest limit: the extraction prompt forbids inferring a strategy, and a test
asserts that wording is present. A model can still misread a chart -- which is
why `unreadable` is part of the returned shape and gets printed.

---

## D-021 — Portal selectors are data, like the instrument registry
Date: 2026-09-21
Context: ability 31 needs data pulled from export portals that have no API. The
obvious implementation is a function per venue.
Options: (a) a function per portal; (b) a `PortalSpec` per portal, as data, with
one generic flow driving all of them.
Chosen: (b). Five portals ship as dict entries: TradingView export, TopstepX
statements, CME DataMine, Databento batch, and a generic broker fallback whose
selectors are the common shapes.
Reasoning: the same argument as hard rule 5 for instruments. These selectors
*will* break -- Section 9 limit 1 says so about TradingView and it is true of
every portal. When one breaks, the fix should be editing a dict entry, not
debugging a function. It also means adding a venue needs no Python at all.
Reversible: yes.
Honest limit: the selector sets are best-effort and unverified against the live
sites, since three of the five need paid accounts. They are built and tested
against mock pages that write real files, so the flow, the download wait, the
paywall branch and the ingestion handoff are all exercised; the selector strings
themselves are the part that needs correcting on first live contact.

---

## D-022 — Options resolve to ordinary Instruments
Date: 2026-09-21
Context: "Any asset, any asset class. Explicitly not narrowed to one." I had
options marked done on the strength of the source registry listing the asset
class, while no option actually loaded, priced or backtested. Free chains exist,
so "no free source" was not a defensible reason -- it was an omission I had
stopped looking at. Found by re-reading the owner's list line by line instead of
trusting my own status file.
Options: (a) an options subsystem with its own backtester; (b) resolve an option
contract to an ordinary `Instrument` and let the existing engine run it.
Chosen: (b). `option_instrument()` turns an OCC symbol into an `Instrument`
whose `tick_value` is `tick_size * multiplier`, and everything downstream --
backtester, fill model, cost model, parity harness, position ledger -- runs
unchanged. What is genuinely option-specific (parsing, greeks, chain selection,
liquidity) lives in `instruments/options.py` and `data/options.py`, which is the
same shape as the futures rollover logic.
Reasoning: (a) would have meant a second engine, and a second engine is a second
set of bugs plus a second thing the parity harness has to be taught about. Hard
rule 5 says asset behaviour is data; an option is an instrument with a 100x
multiplier and an expiry.
Reversible: yes.
Two things worth knowing:
- An option carries its UNDERLYING's correlation group, so a long SPY call on
  one account and a short ES future on another is caught as the hedge it is.
- Single-contract candles are repriced from the underlying when no vendor
  history is available, and both the log line and `Bars.source` say "MODELLED
  prices, not traded option prices". A result built on them is a result about
  the underlying's path, not about option liquidity, and saying so is Law One
  applied somewhere it would have been easy to skip.

---

## D-023 — The Python EMA now seeds the way Pine seeds it
Date: 2026-09-21
Context: the option parity test was the first spec in the project to use
`ma_cross`, and it diverged -- Pine fired the cross two bars before Python. The
cause was seeding. Pine's `ta.ema` starts from a simple average of the first
`length` bars; my Python `_ema` started from `values[0]` and walked forward.
Both are common conventions, the difference decays, and it never vanishes.
Options: (a) change the Pine interpreter to match Python; (b) change Python to
match Pine.
Chosen: (b). Pine is what the client pastes into TradingView, so where the two
disagree, Pine is the specification and the Python engine has to match it.
Reasoning: this is exactly the failure Law Two exists to catch, and it sat
undetected because no spec had used a moving average until now. It is also the
argument for interpreting the emitted Pine rather than comparing the IR to
itself: an IR-to-IR check would never have found it.
Reversible: yes, but the two seeds must agree either way.
Note: no existing result changes. The Sweep Return spec uses no moving average,
and the full parity suite still agrees on all five instruments.

---

## D-024 — The desktop app is stdlib-only, localhost-only
Date: 2026-09-22
Context: "Extremely user friendly, while still being maximally capable." The
agent had a CLI and nothing else. The owner asked how a client actually talks to
it and whether there was a desktop interface, and the honest answer was no. A
terminal is not "extremely user friendly" for a trading client who does not live
in one, and "any client can download it and run it" is not satisfied by
`pip install -e .` followed by remembering command names.
Options: (a) leave it a CLI and document it better; (b) Electron or Tauri;
(c) a web UI on Flask or FastAPI; (d) a local page served by `http.server` from
the standard library.
Chosen: (d). `ee-agent app` starts a loopback server and opens the browser on a
single page: chat, live view of what the agent is doing, running spend, voice
toggle and a microphone button.
Reasoning: (b) means a 150MB download and a build toolchain for a product whose
whole premise is that a client installs it themselves. (c) adds a dependency to
a project that deliberately runs its entire engine on numpy, pandas and pyyaml.
(d) costs nothing: it works on any machine that can already run the agent, and
the zero-cost floor is untouched -- the window opens, answers and runs tools with
an empty keychain, which is an acceptance check.
Reversible: yes; it is an additive package plus one CLI command.

Two things that are not negotiable in this module:
- **It binds to 127.0.0.1 and refuses anything else**, with an error saying why.
  This app has the client's keychain and broker adapters behind it.
- **Every API call carries a session token** minted at startup and embedded in
  the page. Without it, any web page the client happened to have open could
  drive their trading agent from the same machine. Both are acceptance checks.

The app exposes exactly the same 20 tools as `ee-agent chat`, which means the
same guarantee holds: nothing the client can click reaches the order layer.
Order placement stays behind the position ledger and the autonomy ladder, and a
test asserts the window exposes no order verb.

---

## D-025 — NASH Breaker Block v2 replaces Sweep Return in the founder's library
Date: 2026-09-26
Context: The owner asked to swap the Sweep Return strategy for his own published
TradingView indicator, NASH Breaker Block v2 ENTRY SIGNALS
(tradingview.com/script/bpH84GWN). It is his script (author everevolving365) and
open source, so it belongs in the founder's library and its exact source can be
kept alongside as the regression baseline Section 11 asks for.
Options: (a) re-describe the strategy in words and capture it like a client
would; (b) port the script's rules exactly, from its code, with its defaults;
(c) keep both entries.
Chosen: (b). `library/nash-breaker-block-v2/` holds the spec, the original
script byte for byte (`original/`, checksummed), the four generated artifacts,
and a plain-English description that says plainly it was read from the code
and is not the owner's own write-up. `library/sweep-return-v1/` is removed.
Reasoning: the script IS the owner's statement of the strategy, so porting from
it infers nothing. Describing it in words first would have put a lossy step
between the owner and his own rules.
What the script does not say, the spec does not invent (hard rule 1). The script
draws entries only. Stop and target are taken from the owner's TopstepX bracket
(about $60 risk and $100 profit on one MNQ contract: 30 and 50 points), logged
as PENDING assumptions with the other readings (15/25 points if that bracket
applied to the 2-contract position the old bot traded), together with the time
filter, the daily cap and the session-close rule. The spec backtests; the
validator blocks it from live capital until the owner approves or replaces them.
Kept: `tests/fixtures/specs/sweep-return-atr.yaml` stays as internal test data.
Dozens of tests and several acceptance checks are calibrated on it, and it
exercises primitives NASH does not use (session ranges, returns inside,
session-end invalidation). It is not offered to anyone.
Reversible: yes; the old entry is in git history.

---

## D-026 — The owner's original script is a fifth parity target
Date: 2026-09-26
Context: For a strategy ported from a script, "the strategy you described and
the strategy trading your account are provably the same object" means the port
must be provably the script. Comparing the four generated targets with each
other proves they agree; it does not prove they agree with what the owner has
on his chart.
Chosen: the Pine interpreter was extended until it runs the owner's v6 script
unmodified (user functions with local scope, `for ... to` counting both ways
with `break`, arrays, tuples, `input.*` defaults and overrides by title,
`request.security` on intraday higher timeframes, `ta.pivothigh/low`, `hour()`
and `minute()` with a timezone, drawing calls as no-ops). The harness finds
`original/manifest.yaml`, runs the script and compares its `bosLong`/`bosShort`
against the Python engine bar for bar.
Two practical details:
- The script keeps every 15-minute gap it ever saw and re-checks all of them on
  every bar, so its cost grows with history. Over a long dataset the harness
  runs it on the most recent 2,500 bars and re-runs the Python engine over those
  same bars from the same first bar. Short datasets include it as a fifth
  fingerprint directly.
- The checksum is taken over the script with LF line endings. A Windows
  checkout turns LF into CRLF; that is not a change to the script, and without
  this the byte check failed on the machine it was written on. `.gitattributes`
  now pins `*.pine` to LF as well.
Result: all five agree on the committed MNQ data (518 signals over 16,415 bars
for the four generated targets; the original agrees on its window), and on
round-the-clock synthetic data with every optional switch in the script flipped
on in turn.
Reversible: yes; `run_parity(..., original=None)` skips it.

---

## D-027 — Four TradingView semantics, pinned down
Date: 2026-09-26
Each of these changes signals, and each is now defined once and shared by the
Python primitives and the interpreter so the two cannot drift.
1. **Pivot ties.** `ta.pivothigh(high, 3, 3)`: an equal high on the LEFT does
   not disqualify a candidate, an equal high on the RIGHT does. Of two equal
   highs the later one is the pivot. (Source: TradingView's behaviour as
   reproduced by LuxAlgo's PineTS, PR #322.) Pivot lows mirror it.
2. **Higher-timeframe timing** (`ee_agent/data/htf.py`). With
   `lookahead_off`, a historical chart bar sees the latest 15-minute bar that
   had closed by its own close: the 09:00 to 09:15 bar reaches a 5-minute chart
   on the 09:10 bar. A missing closing bar delays it to the next bar. Daily
   bars are refused rather than rebuilt from midnight, because a futures daily
   bar follows the exchange session. `lookahead_on` is refused outright.
3. **`and`/`or` evaluation.** Pine v6 short-circuits; Pine v5 evaluates both
   sides. The interpreter short-circuited everything, which is wrong for every
   v5 script we emit. Harmless for the primitives we had (no built-in with state
   sat on the right of an `and`), but a latent parity bug; now version-aware.
4. **`request.security_lower_tf`** has no 1-minute data behind it here. It
   returns a one-element array with the chart bar's own value, which is exact
   for anything read at a chart-bar boundary. NASH reads opens at 17:00, 23:00
   and 08:30, all on 5-minute boundaries, so the approximation is exact for it.
   The run records a note whenever it is used, and the `session_open_levels`
   primitive refuses a chart timeframe that does not divide its anchors.

---

## D-028 — Fuel is defined on the break bar; distances inside rules are portable too
Date: 2026-09-26
Context: two details of the port where the obvious implementation was wrong.
1. `fvg_fuel` is "the level broke on this bar AND a qualifying gap exists", and
   the gap search runs only on a break bar, in Python and in the emitted Pine
   (as a ternary, which is lazy in every Pine version). The owner's v6 script
   evaluates it the same way. Pine v5 would otherwise evaluate the search on
   every bar, a real cost on TradingView and 16,000 function calls per run in
   the interpreter. The validator requires the `structure_break` in the same
   rule's `all_of`, so gating changes no result.
2. Mitigation in the generated script is tracked only while a gap can still be
   read (the largest `max_age` referencing it). An older gap is never read
   again, so the result is identical, and TradingView's per-bar loop stays
   short. The original script re-checks every gap forever, which is why it is
   the slow target.
3. Portability now covers distances inside entry rules, not only risk. "A gap
   within 120 points of the level" is 60% of a typical MNQ day and 240% of an ES
   day. `ee-agent spec portable` measures the ATR(14) equivalent and shows it for
   approval, exactly as it does for stops and targets.

---

## D-029 — The app's data lives in a per-user folder, never in the program
Date: 2026-09-26
Context: `EE_HOME` defaulted to the repository itself, so every run wrote its
ledgers into the clone: the position ledger, the research index, conversation
transcripts, kill-switch events, overnight reports. Verify runs had been
committing them. Anyone who downloaded the agent inherited the developer's
test positions and history. For an app a client installs, that is a bug and a
privacy problem.
Chosen: `ee_home()` now defaults to `paths.user_data_dir()`:
`%USERPROFILE%\EverEvolving` on Windows (see D-030 for why not AppData),
`~/Library/Application Support/EverEvolving` on macOS and
`$XDG_DATA_HOME/everevolving` on Linux. `EE_HOME` still overrides it. The
runtime files are removed from git and listed in `.gitignore`, anchored to the
repository root so the library's own `generated/` folders stay tracked. The
indicator the app compiles now goes to the data folder as well. An acceptance
check fails if runtime history is ever committed again.
Reversible: yes; set `EE_HOME` to anything.

---

## D-030 — An installable desktop app: an app window, a windowed launcher, one-step installers
Date: 2026-09-26
Context: The owner wants anyone to be able to give their Claude the GitHub
link, say "install this", and end up with a desktop app. Until now that meant
Python, a terminal, `pip install -e .` and `ee-agent app`, which opens a browser
tab.
Options: (a) Electron or Tauri; (b) a frozen executable (PyInstaller); (c) the
existing local server shown in an app-mode browser window, started by a
windowed launcher, and installed by a script that builds a private
environment and a real shortcut.
Chosen: (c).
- `ee-agent-desktop` is a `gui-scripts` entry point, so it runs with no console
  window. It opens Edge or Chrome with `--app=` and a profile of its own. That
  means no address bar or tabs, its own taskbar entry, and the app icon. Edge
  ships with every Windows 10 and 11 machine. If no Chromium browser exists it
  falls back to the default browser.
- A second launch finds the running app (`app.json` plus an authenticated
  `/api/ping`) and opens another window on it instead of starting a second
  server.
- The page sends a heartbeat every 5 seconds. After 25 seconds without one, the
  app stops by itself, so closing the window really closes the app. A Close
  button does the same at once.
- The icon is drawn in code (`ee_agent/ui/icon.py`), so there is no binary in
  the repository.
- `Install.bat` / `install/windows/install.ps1`, `install.sh` and
  `Install.command` for the Mac, plus `bootstrap.ps1` for a one-line install
  with no git. `CLAUDE.md` tells Claude exactly what to run and never to handle
  the person's keys.
Reasoning: (a) means a 150MB runtime and a build toolchain, against D-024.
(b) produces a large unsigned executable that antivirus tools flag and that
must be rebuilt for every change. (c) reuses the app that already exists and
is tested, updates with a `git pull`, and needs nothing but Python, which the
installer fetches if it is missing.
Found by running it here: when the installer runs inside a packaged (MSIX) app,
and the Claude desktop app is one, Windows silently redirects everything
written under `%LOCALAPPDATA%` into that app's private sandbox. The first
install on this machine put the environment inside Claude's sandbox, and the
Desktop icon pointed at a folder nothing else could see. That is exactly the
"ask Claude to install it" case, so the install and data folder moved to
`%USERPROFILE%\EverEvolving`, which is never redirected. The installer now
checks whether its Start menu entry was redirected and says so. The Desktop is
never redirected.
Honest limits: tested end to end on Windows 11 here: install, icon, window,
second launch, closing, quitting. The macOS and Linux installers are written
and syntax-checked but have not been run on those systems. The app window
needs Edge, Chrome, Brave or Chromium for the no-address-bar look; without one
it opens in the default browser.
Reversible: yes; `install\windows\uninstall.ps1` removes the shortcuts and the
environment, and keeps the data unless told otherwise.
