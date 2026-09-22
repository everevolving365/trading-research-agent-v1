# BUILD LOG
Last updated: 2026-09-22 (phase 16)

## CURRENT STATE
Phase: 16 — The desktop app — **acceptance PASSED**
Status: sixteen phases complete. `make verify` runs 62 acceptance checks on a
clean clone with zero credentials; `make test` runs 285 tests.
**Every ability on the owner's requirement list is built.** `docs/STATUS.md`
reports 79 of 83 `done`, 3 `runtime` (need the owner's own account to exercise
live), and exactly 1 `partial`: ability 65's CPI dates, which needs a refresh
from the BLS calendar rather than code.

Next action: nothing is blocking. The highest-value next step is still not more
code — it is running the Operator against the real TradingView site once B-001
lands, because that is the only part of the system no fixture can validate.

## HOW TO RESUME COLD
Say "continue from BUILD-LOG.md". Then:

1. The clone lives in the **`repo` subfolder**, not in "Day trader" directly:
   `Desktop/Day trader/repo`
2. `git pull` — origin/main is always current; every phase is pushed.
3. `make verify` to confirm the state you inherited (about 12 minutes).
4. Read OPEN THREADS at the bottom of this file and pick the top item.

## PHASE STATUS
- [x] Phase 0 — Foundation and autonomy infrastructure — acceptance PASSED 2026-09-17
- [x] Phase 1 — The spec, the compilers, the cost notifier — acceptance PASSED 2026-09-17
- [x] Phase 2 — Instrument registry and data layer — acceptance PASSED 2026-09-17
- [x] Phase 3 — Law One, the truth engine — acceptance PASSED 2026-09-17
- [x] Phase 4 — Capture — acceptance PASSED 2026-09-17
- [x] Phase 5 — Law Two, parity — acceptance PASSED 2026-09-17
- [x] Phase 6 — The Operator — acceptance PASSED 2026-09-17 (mock page set; B-001)
- [x] Phase 7 — Law Three, safety — acceptance PASSED 2026-09-17
- [x] Phase 8 — Live execution — acceptance PASSED 2026-09-17 (replay harness; B-002)
- [x] Phase 9 — Order flow — acceptance PASSED 2026-09-17
- [x] Phase 10 — Autonomy and research — acceptance PASSED 2026-09-17
- [x] Phase 11 — Moat and polish — acceptance PASSED 2026-09-17
- [x] Phase 12 — Completeness sweep — acceptance PASSED 2026-09-17
- [x] Phase 13 — Conversation and source discovery — acceptance PASSED 2026-09-18
- [x] Phase 14 — Screenshot intake and export portals — acceptance PASSED 2026-09-21
- [x] Phase 15 — Options as an asset class — acceptance PASSED 2026-09-21
- [x] Phase 16 — The desktop app — acceptance PASSED 2026-09-22

## HOW TO CHECK THE BUILD YOURSELF

```bash
make verify        # 62 acceptance checks, every phase, zero credentials, ~13 min
make test          # 285 tests, ~14 min
ee-agent app       # THE DESKTOP APP -- start here, no terminal knowledge needed
ee-agent demo      # a real backtest plus a parity proof, no keys
ee-agent chat      # the same conversation in a terminal; 20 tools
ee-agent sources "1-minute copper futures history"
ee-agent screenshots my-charts/*.png
ee-agent portal list
ee-agent options chain SPY --delta 0.25 --tradeable
```

The single most important line of output:

```
[parity] sweep-return-v1 on MNQ 5m over 17,061 bars
    python           f6b93e53d785    1068 signals
    pine_indicator   f6b93e53d785    1068 signals
    pine_strategy    f6b93e53d785    1068 signals
    live             f6b93e53d785    1068 signals
    AGREED -- all 4 targets produced the identical fingerprint f6b93e53d785.
```

That is the one-sentence test, answered. It holds on MNQ, ES, SPY, EURUSD and
BTCUSDT — futures, equity, forex and crypto, in three exchange timezones.

## COMPLETED

**Phase 0** — `secrets/` with `get_secret()` over OS keychain → encrypted file →
environment; `.env.example` and `config/client.example.yaml` listing every key,
what it unlocks and where to get it; cost notifier that informs and never
blocks; research index written on every run; cache age printed on every load;
LICENSE; README title and install path fixed; v1 archived to `legacy/v1/`.

**Phase 1** — the strategy spec as the single source of truth (versioned,
diffable, dimensionless, with an explicit assumptions block); validator with the
interrogation completeness checklist; 13 primitives each implemented across
Python, Pine indicator, Pine strategy and live, with a registry audit that fails
the build if any target is missing; all four compilers; cost estimation wired in
from the start.

**Phase 2** — instrument registry as the only place asset behaviour lives; data
source registry with a capability matrix and a fallback ladder; integrity engine
(gaps, duplicates, out-of-order, DST, splits, continuous stitching, short
sessions, quality score); universal ingestion; cache with visible age; paywall
handling; ~10MB of committed fixtures so the entire system runs offline.

**Phase 3** — event-driven backtester that cannot run without costs; slippage as
a function of volume and volatility; partial fills, queue position, stop-run and
gap modelling; walk-forward (anchored and rolling); Monte Carlo; regime
decomposition; synthetic markets; selection correction; lookahead detector;
adversarial pass with 13 computed attacks; three prop rule packs with a combine
simulator that models intraday excursion.

**Phase 4** — deterministic transcript parser needing no model; interrogation
engine; ambiguity ledger with measured sensitivity; visual confirmation loop
driven by a synthetic approver; natural language editing with before/after; both
optional intake paths.

**Phase 5** — a Pine v5 interpreter that parses and executes the *emitted*
script bar by bar, so parity compares real artifacts rather than one code path
against itself; signal fingerprinting; divergence located to the bar; pinned,
byte-reproducible artifacts; edge-case generation.

**Phase 6** — the Operator, built against a local mock TradingView page set:
login, Pine paste/save/add-to-chart, Strategy Tester, Deep Backtesting with plan
detection, report parsing, alerts and webhooks, screenshot audit trail,
human-confirmed publishing. Order placement is physically absent and a test
greps for it.

**Phase 7** — sandbox with no network, no secrets, one writable directory and a
hard timeout, refusing anything unapproved; global position ledger with
correlation-aware hedge refusal before any API call; autonomy ladder; kill
switches at three levels.

**Phase 8** — adapter interface, mock reference adapter, TopstepX adapter
contract-tested against a mock server, order router where every order passes
kill switches → autonomy → prop limits → hedge check, replay harness with an
injectable clock, drift monitoring, execution scorecard, shadow mode.

**Phase 9** — order flow primitives on real trade data, degrading to bar-derived
proxies and reporting the degradation.

**Phase 10** — overnight loop, hypothesis queue, cross-asset scanner, event
calendar, session narration, morning brief, evening debrief, tearsheets.

**Phase 11** — verified signal ledger (hash chain, outcome excluded from the
hashed payload), founder's library offered once, one-command install, first-run
wizard, free-tier demo.

**Phase 12** — `ee_agent/verify.py`, `docs/STATUS.md`, this file, DECISIONS.md
and BLOCKERS.md.

**Phase 13** — *Conversation* (`ee_agent/conversation/`, D-018). `ee-agent chat`
is a tool-using agent, not a chat window — 18 tools covering capture,
interrogation, data loading and discovery, screenshots, portals, backtest,
compile, parity, the Operator, order flow, the library, the research index and
spend. Anthropic, OpenAI, Gemini, or none. Two guard rails in code rather than in
the prompt: no conversational tool can reach the order layer (a test greps for
it), and `answer_question` refuses a non-answer like "whatever you think is
best" rather than inventing a risk rule.
*Source discovery* (`ee_agent/data/discovery.py`, D-019) — `ee-agent sources`
searches a curated 16-vendor catalogue offline with no key, plus live web search
through the client's own Brave/Tavily/SerpAPI key.

**Phase 14** — *Screenshot intake* (`ee_agent/capture/vision.py`, D-020).
`ee-agent screenshots` reads the markup with a vision model and returns levels,
arrows, zones and annotations — then builds a spec proposal in which every
inferred element is an UNAPPROVED assumption, because a picture shows where the
client entered and not why.
*Export portals* (`ee_agent/operator/portals.py`, D-021) — five portals as data,
one generic flow: login, fields, export, download, handoff to ingestion. A
purchase gate routes into the paywall handler.

**Phase 16** — *The desktop app* (`ee_agent/ui/`, D-024). `ee-agent app` opens a
window: chat, a live view of what the agent is doing, the running spend, a voice
toggle and a microphone button. Built on `http.server` from the standard library
— no Flask, no node, no build step — so it works anywhere the agent already
runs. Binds to 127.0.0.1 and refuses anything else; every API call carries a
session token, because this app has the client's keychain behind it. Exposes the
same 20 tools as `ee-agent chat`, so nothing the client can click reaches the
order layer.

**Phase 15** — *Options* (`ee_agent/instruments/options.py`,
`ee_agent/data/options.py`, D-022). An option contract resolves to an ordinary
`Instrument`, so the whole engine runs it with no asset-class branch. Parsing
accepts OCC, prefixed and plain-English forms; Black-Scholes greeks are verified
against put-call parity; chains come from yfinance, Polygon or a synthetic
offline surface, with at-the-money and by-delta selection and a liquidity filter.
An option carries its underlying's correlation group, so a long SPY call against
a short ES future is caught as a hedge. Single-contract candles are repriced from
the underlying when no vendor history exists, and labelled as modelled.

## BUGS THE BUILD'S OWN CHECKS CAUGHT

Worth reading, because each was silent and each would have cost money:

1. **`session_end` fired on the last array element** — making signals depend on
   how much history was loaded. Caught by our own lookahead detector. (D-005)
2. **The level-4 daily loss cap was blocking exits** — trapping the client in
   the position the limit existed to protect them from. (D-006)
3. **Pine line-level dedup orphaned `if` bodies** — emitting a script that would
   not compile. Caught by the Pine interpreter. (D-007)
4. **Session windows were evaluated in the exchange's timezone, not the
   strategy's** — so "09:30 to 15:00 Central" ran an hour off on SPY. Caught by
   the parity harness on the first non-Central instrument tested. (D-013)
5. **The integrity engine divided a raw int64 timestamp view by 60e9** assuming
   nanoseconds; pandas 3 stores microseconds, so every gap was scaled by 1/1000
   and the check reported clean on a dataset with a hole in it. (D-014)
6. **A whole missing trading day was forgiven as an overnight break**, and a
   truncated session scored 1.00. Both now detected. (D-016)
7. **The source ladder ranked fixtures above live sources** — the agent would
   have served recorded data instead of the market. (D-015)
8. **`\b` does not match before an underscore**, so `MNQ_5m_2026-03-02.png` had
   its symbol read as unknown.
9. **`takeprofit.yaml` did not parse at all** — `- Note: text` in a YAML list is
   a mapping key, not a string. Nothing noticed because the test only loaded
   Topstep. (D-017)
10. **A test that set a spend ceiling left it set for every test after it** —
    the cost ledger is a process global.
11. **The vision cost charge sat inside the provider's `read()`**, so a
    substituted reader was silently unmetered. Moved to the orchestrator.
12. **The Python EMA seeded from `values[0]`; Pine's `ta.ema` seeds from an SMA
    of the first `length` bars.** The difference decays but never vanishes, and
    it moved a cross by two bars. Undetected until the first spec that used a
    moving average — and it is the reason parity interprets the emitted Pine
    instead of comparing the IR to itself. (D-023)
13. **The chain generator produced duplicate expiries**, so the same contract
    appeared twice with different open interest. A client picking "the 560 call"
    would have seen two of them.
14. **The committed fixtures age.** A request for "the last 60 days" had begun
    falling entirely outside them, which read as "no data exists" rather than
    "your window has moved past the recording".
15. **There was no way for a non-technical client to use any of it.** Everything
    worked and it all lived behind a terminal. The owner asked how a client
    actually talks to the agent; the answer was a CLI, which does not satisfy
    "extremely user friendly". (D-024)

## OPEN THREADS

Nothing is blocking, and there is no ability left that more code can complete.
Ordered by value:

- **Run the Operator against the real TradingView site** once B-001 lands. This
  is the only part of the system no fixture can validate. Expect selector
  corrections — they are all in one `SELECTORS` dict for exactly that reason.
- **Run the portals against their live sites.** Same argument; `PORTALS` is one
  dict entry per venue. Three of the five need paid accounts.
- **Drop the originals from B-003** into `library/sweep-return-v1/original/` and
  the signal-count delta gets reported in `docs/STATUS.md`.
- **Refresh `research/calendar.json` CPI dates** from the BLS schedule (B-007).
  FOMC and NFP are already exact.
- **Add a fetcher for whichever vendor the client picks** from `ee-agent
  sources`. The interface is `Fetcher` in `ee_agent/data/sources.py`; each one
  is about 40 lines.
- **Option chains** currently come from yfinance (free) or Polygon (keyed). If
  the client wants tick-level option data, Databento and Tardis are in the
  source catalogue and each needs about 40 lines of fetcher.
