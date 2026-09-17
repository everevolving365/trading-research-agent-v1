# BLOCKERS

Everything the build genuinely cannot resolve on its own. Each one names exactly
what is needed from the owner, in one sentence, and what was built anyway so the
build never stopped.

None of these blocked a phase. Every one has a working code path against a stub,
a fixture or a mock, with tests.

---

## B-001 — TradingView Premium account for live Operator validation
**Blocks:** live validation of the Operator against the real TradingView site
(Phase 6 acceptance is satisfied against the mock page set).
**Needs from owner:** a TradingView Premium login stored via
`ee-agent secrets set TRADINGVIEW_USERNAME` / `TRADINGVIEW_PASSWORD`, or a
browser profile path to attach to.
**Workaround built:** a complete local mock of TradingView's structure
(`tests/fixtures/tradingview/`) plus `MockPageDriver`. The whole flow — login,
Pine paste, save to account, add to chart, Strategy Tester, Deep Backtesting,
report parse, alert and webhook creation — runs end to end against it, and the
report parser is tested against both a regular and a deep report. `PlaywrightDriver`
implements the identical surface for the real browser.
**Impact if never resolved:** selectors will need correcting the first time it
runs against the live site, and Deep Backtesting stays unavailable (it is gated
behind Premium by TradingView, not by us). Nothing else is affected: the Python
engine is authoritative and the deep backtest is a cross-check.
**Status:** OPEN

---

## B-002 — The owner's existing Python trading robot
**Blocks:** nothing. Phase 8 completes against the mock broker.
**Needs from owner:** the robot's source, or an object exposing the seven
methods in `docs/ADAPTER-INTERFACE.md`.
**Workaround built:** `BrokerAdapter` interface, `MockBroker` reference
implementation, `OwnerRobotAdapter` that forwards to the robot and raises with
the interface document's path until it is supplied, and a full contract test
suite that any adapter can be run through.
**Impact if never resolved:** none to the system. TopstepX is already
implemented; the owner's robot is an additional adapter, not a dependency.
**Status:** OPEN

---

## B-003 — The two EE Sweep Return `.pine` files and `how-i-trade-it.md`
**Blocks:** the signal-count delta between the generated scripts and the
owner's originals, which Section 11 asks for "for information only".
**Needs from owner:** `EE-Sweep-Return-v1.pine` and
`EE-Sweep-Return-STRATEGY-v1.pine` dropped into
`library/sweep-return-v1/original/`, plus the write-up.
**Workaround built:** the library entry is a complete spec built from the
strategy description in Section 11 and the owner's authoritative ruling that the
two files are the same strategy with bar-close confirmation on the strategy
script. It compiles to all four targets and the parity harness proves they
agree. `library.loader.signal_count_delta()` reports the absence explicitly
rather than skipping it silently.
**Impact if never resolved:** no regression baseline against the original
scripts. The generated scripts are still proven consistent with each other and
with the Python engine.
**Status:** OPEN

---

## B-004 — Real client credentials for paid data sources
**Blocks:** nothing. Every paid fetcher is built and tested.
**Needs from owner (or client):** `POLYGON_API_KEY` and/or `DATABENTO_API_KEY`
via `ee-agent secrets set`.
**Workaround built:** every paid source is built against recorded fixture
responses committed to `tests/fixtures/data/`. The fetchers are complete; they
serve the fixture and say so in the source name when no key is present. Keyless
crypto (Binance public klines) and universal file ingestion cover the zero-cost
floor.
**Impact if never resolved:** live futures/equity/forex data is unavailable, and
real order flow degrades to bar-derived proxies — which the flow layer reports
on every call rather than hiding.
**Status:** OPEN

---

## B-005 — Live market sessions
**Blocks:** nothing, by design.
**Needs from owner:** nothing. This is listed because Section 2.2 names it.
**Workaround built:** the replay harness feeds recorded history through the
identical live code path — live plan, router, position ledger, autonomy ladder,
kill switches, broker adapter — in accelerated time, with an injectable clock so
per-day caps and counters behave exactly as they would live. Every live-execution
acceptance criterion is satisfied against it.
**Impact if never resolved:** none to the build. Real capital obviously requires
real sessions, gated behind the autonomy ladder.
**Status:** RESOLVED (by design — no phase is ever gated on market time)

---

## B-006 — Real human approval in the visual confirmation loop
**Blocks:** nothing.
**Needs from owner:** nothing at build time; a client at runtime.
**Workaround built:** the loop is driven by an `Approver` interface.
`SyntheticApprover` accepts or rejects according to a fixture file, and the
build verifies the loop end to end including that rejections revise the spec
correctly (a consistently rejected side is removed; rejections clustered early
in the session move the window start). `ConsoleApprover` is the real one.
**Impact if never resolved:** none.
**Status:** RESOLVED (by design)

---

## B-007 — Exact economic release dates
**Blocks:** precision of the CPI/PCE blackout windows.
**Needs from owner:** nothing; this is a data refresh task.
**Workaround built:** `ee_agent/research/calendar.json` carries FOMC dates
(exact, published in advance) and NFP (first Friday, deterministic). CPI dates
are approximated to the second Wednesday and the file says so in
`coverage_note`, which also states plainly that unscheduled news cannot be in
there at all.
**Impact if never resolved:** a CPI blackout may be a day off. FOMC and NFP are
correct.
**Status:** OPEN
