# BUILD LOG
Last updated: 2026-09-17

## CURRENT STATE
Phase: 12 — Completeness sweep — **acceptance PASSED**
Status: all twelve phases complete. `make verify` runs 41/41 green on a clean
clone with zero credentials; `make test` runs 172/172.
Next action: nothing is blocking. When the owner supplies any item from
BLOCKERS.md, wire it in — start with B-003 (the two original `.pine` files),
which is the smallest and unlocks the regression baseline in `docs/STATUS.md`.

## PHASE STATUS
- [x] Phase 0 — Foundation and autonomy infrastructure — acceptance PASSED 2026-09-17
- [x] Phase 1 — The spec, the compilers, the cost notifier — acceptance PASSED 2026-09-17
- [x] Phase 2 — Instrument registry and data layer — acceptance PASSED 2026-09-17
- [x] Phase 3 — Law One, the truth engine — acceptance PASSED 2026-09-17
- [x] Phase 4 — Capture — acceptance PASSED 2026-09-17
- [x] Phase 5 — Law Two, parity — acceptance PASSED 2026-09-17
- [x] Phase 6 — The Operator — acceptance PASSED 2026-09-17 (against the mock page set; B-001)
- [x] Phase 7 — Law Three, safety — acceptance PASSED 2026-09-17
- [x] Phase 8 — Live execution — acceptance PASSED 2026-09-17 (replay harness; B-002)
- [x] Phase 9 — Order flow — acceptance PASSED 2026-09-17
- [x] Phase 10 — Autonomy and research — acceptance PASSED 2026-09-17
- [x] Phase 11 — Moat and polish — acceptance PASSED 2026-09-17
- [x] Phase 12 — Completeness sweep — acceptance PASSED 2026-09-17

## HOW TO CHECK THE BUILD YOURSELF

```bash
make verify        # 41 acceptance checks, every phase, zero credentials, ~12 min
make test          # 172 tests, ~11 min
ee-agent demo      # a real backtest plus a parity proof, no keys
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

## COMPLETED THIS SESSION

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
(14 entries) and BLOCKERS.md (7 entries).

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
   truncated session scored 1.00. Both now detected.
7. **The source ladder ranked fixtures above live sources** — the agent would
   have served recorded data instead of the market.
8. **`\b` does not match before an underscore**, so `MNQ_5m_2026-03-02.png` had
   its symbol read as unknown.

## OPEN THREADS

None blocking. Ordered by value if the owner wants more built:

- Wire in the originals from B-003 and report the signal-count delta.
- Run the Operator against the real TradingView site once B-001 lands; expect
  selector corrections, which is why they are all in one `SELECTORS` dict.
- Refresh `research/calendar.json` CPI dates from the BLS schedule (B-007).
- A model-backed extractor layered on top of `capture/parser.py` for strategy
  descriptions the deterministic parser cannot reach. The interface is already
  in place: the parser reports what it could not parse.
