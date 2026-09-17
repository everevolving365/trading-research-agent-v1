"""The parity harness: four targets, one strategy, proven.

Runs all four compilations over the same bars and compares their signal
fingerprints:

* **python**           -- the backtest engine's compiled plan
* **pine_indicator**   -- the emitted arrow indicator, executed by the Pine interpreter
* **pine_strategy**    -- the emitted strategy script, same interpreter
* **live**             -- the live config, rebuilt from JSON by the execution layer

This is the answer to the one-sentence test: the strategy the client described
and the strategy trading their account are the same object, and this is the
proof.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ee_agent.compile.to_live import compile_to_live
from ee_agent.compile.to_pine import compile_to_pine_indicator, compile_to_pine_strategy
from ee_agent.compile.to_python import compile_to_python
from ee_agent.execution.live_plan import LivePlan
from ee_agent.instruments.registry import get_instrument
from ee_agent.parity import fingerprint as fp
from ee_agent.parity.pine_sim import PineError, run_pine
from ee_agent.spec.model import StrategySpec


@dataclass
class ParityResult:
    spec_id: str
    spec_hash: str
    symbol: str
    timeframe: str
    n_bars: int
    fingerprints: list[fp.Fingerprint] = field(default_factory=list)
    divergences: list[fp.Divergence] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    @property
    def targets(self) -> list[str]:
        return [f.target for f in self.fingerprints]

    @property
    def agreed(self) -> bool:
        return (
            len(self.fingerprints) >= 2
            and not self.divergences
            and not self.errors
            and len({f.digest for f in self.fingerprints}) == 1
        )

    def to_dict(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "spec_hash": self.spec_hash,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "n_bars": self.n_bars,
            "agreed": self.agreed,
            "targets": {f.target: {"digest": f.digest, "n_signals": len(f.signals)} for f in self.fingerprints},
            "n_divergences": len(self.divergences),
            "divergences": [d.explain() for d in self.divergences[:20]],
            "errors": self.errors,
            "limitations": self.limitations,
        }

    def report(self) -> str:
        lines = [
            f"[parity] {self.spec_id} on {self.symbol} {self.timeframe} over {self.n_bars:,} bars",
        ]
        for f in self.fingerprints:
            lines.append("    " + f.line())
        for target, err in self.errors.items():
            lines.append(f"    {target:<16} FAILED: {err}")
        if self.agreed:
            lines.append(
                f"    AGREED -- all {len(self.fingerprints)} targets produced the identical "
                f"fingerprint {self.fingerprints[0].short}."
            )
        else:
            lines.append(f"    DIVERGED -- {len(self.divergences)} disagreement(s):")
            for d in self.divergences[:10]:
                lines.append(f"        {d.explain()}")
            if len(self.divergences) > 10:
                lines.append(f"        ... and {len(self.divergences) - 10} more")
        for note in self.limitations:
            lines.append(f"    note: {note}")
        return "\n".join(lines)


def run_parity(
    spec: StrategySpec,
    bars,
    targets: tuple[str, ...] = ("python", "pine_indicator", "pine_strategy", "live"),
) -> ParityResult:
    instrument = get_instrument(bars.symbol)
    result = ParityResult(
        spec_id=spec.id,
        spec_hash=spec.hash,
        symbol=bars.symbol,
        timeframe=bars.timeframe,
        n_bars=len(bars),
    )

    if "python" in targets:
        try:
            sig = compile_to_python(spec).evaluate(bars, instrument)
            result.fingerprints.append(
                fp.from_arrays("python", bars, sig.long_entry, sig.short_entry)
            )
        except Exception as exc:
            result.errors["python"] = str(exc)

    for target, compile_fn in (
        ("pine_indicator", compile_to_pine_indicator),
        ("pine_strategy", compile_to_pine_strategy),
    ):
        if target not in targets:
            continue
        try:
            artifact = compile_fn(spec)
            result.limitations.extend(x for x in artifact.limitations if x not in result.limitations)
            series = run_pine(
                artifact.source, bars, ["longSignal", "shortSignal"], mintick=instrument.tick_size
            )
            result.fingerprints.append(
                fp.from_arrays(
                    target,
                    bars,
                    np.asarray(series["longSignal"], dtype=bool),
                    np.asarray(series["shortSignal"], dtype=bool),
                    meta={"source_hash": artifact.hash},
                )
            )
        except PineError as exc:
            result.errors[target] = f"interpreter does not implement: {exc}"
        except Exception as exc:
            result.errors[target] = str(exc)

    if "live" in targets:
        try:
            cfg = compile_to_live(spec).to_dict()
            live = LivePlan(cfg).evaluate(bars, instrument)
            result.fingerprints.append(
                fp.from_arrays("live", bars, live.long_entry, live.short_entry)
            )
        except Exception as exc:
            result.errors["live"] = str(exc)

    result.divergences = fp.compare(result.fingerprints, bars=bars)
    return result


def explain_divergence(spec: StrategySpec, bars, divergence: fp.Divergence) -> str:
    """Why this bar disagreed: every condition's value on it, per target."""
    i = divergence.bar_index
    if i < 0:
        return "bar not found in the dataset"
    compiled = compile_to_python(spec)
    sig = compiled.evaluate(bars, get_instrument(bars.symbol))
    lines = [
        f"bar {i} {divergence.ts}  O {bars.open[i]} H {bars.high[i]} L {bars.low[i]} C {bars.close[i]}",
        f"  python long={bool(sig.long_entry[i])} short={bool(sig.short_entry[i])} "
        f"filter={bool(sig.filter_ok[i])}",
    ]
    for key, arr in sorted(sig.debug.items()):
        try:
            lines.append(f"  {key:<40} {arr[i]}")
        except Exception:
            continue
    return "\n".join(lines)
