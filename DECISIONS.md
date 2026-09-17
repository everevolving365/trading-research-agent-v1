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
