"""The parity harness: four targets, one strategy, proven.

Runs all four compilations over the same bars and compares their signal
fingerprints:

* **python**           -- the backtest engine's compiled plan
* **pine_indicator**   -- the emitted arrow indicator, executed by the Pine interpreter
* **pine_strategy**    -- the emitted strategy script, same interpreter
* **live**             -- the live config, rebuilt from JSON by the execution layer
* **original**         -- for a strategy ported from a script, the owner's own
  script, unmodified, through the same interpreter (:mod:`ee_agent.parity.original`)

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
from ee_agent.parity.original import OriginalScript, original_for
from ee_agent.parity.pine_sim import PineError, run_pine
from ee_agent.spec.model import StrategySpec

ALL_TARGETS = ("python", "pine_indicator", "pine_strategy", "live", "original")


@dataclass
class OriginalCheck:
    """The owner's original script against the Python engine.

    ``windowed`` means the script ran over the most recent ``n_bars`` only (its
    own cost grows with history) and the Python engine was re-run over exactly
    those bars from the same starting bar, so the comparison is like for like.
    """

    script: OriginalScript
    n_bars: int
    windowed: bool
    python: fp.Fingerprint | None = None
    original: fp.Fingerprint | None = None
    divergences: list[fp.Divergence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def agreed(self) -> bool:
        return (
            not self.error
            and self.python is not None
            and self.original is not None
            and not self.divergences
            and self.python.digest == self.original.digest
        )

    def to_dict(self) -> dict:
        return {
            "file": self.script.path.name,
            "sha256": self.script.sha256,
            "n_bars": self.n_bars,
            "windowed": self.windowed,
            "agreed": self.agreed,
            "digest": self.original.digest if self.original else None,
            "n_signals": len(self.original.signals) if self.original else 0,
            "divergences": [d.explain() for d in self.divergences[:20]],
            "error": self.error,
            "notes": self.notes,
        }


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
    original: OriginalCheck | None = None

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
            and (self.original is None or self.original.agreed)
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
            "original": self.original.to_dict() if self.original else None,
        }

    def report(self) -> str:
        lines = [
            f"[parity] {self.spec_id} on {self.symbol} {self.timeframe} over {self.n_bars:,} bars",
        ]
        for f in self.fingerprints:
            lines.append("    " + f.line())
        for target, err in self.errors.items():
            lines.append(f"    {target:<16} FAILED: {err}")
        main_agreed = (
            len(self.fingerprints) >= 2
            and not self.divergences
            and not self.errors
            and len({f.digest for f in self.fingerprints}) == 1
        )
        orig = self.original
        if orig is not None and orig.windowed:
            lines.extend(self._original_lines(orig))
        if main_agreed:
            names = "targets"
            if any(f.target == "original" for f in self.fingerprints):
                names = "targets, including the owner's own script,"
            lines.append(
                f"    AGREED -- all {len(self.fingerprints)} {names} produced the identical "
                f"fingerprint {self.fingerprints[0].short}."
            )
            if orig is not None and orig.windowed and orig.agreed:
                lines.append(
                    f"    AGREED -- the owner's own script ({orig.script.path.name}, unmodified) matches "
                    f"the Python engine signal for signal over the most recent {orig.n_bars:,} bars."
                )
        else:
            lines.append(f"    DIVERGED -- {len(self.divergences)} disagreement(s):")
            for d in self.divergences[:10]:
                lines.append(f"        {d.explain()}")
            if len(self.divergences) > 10:
                lines.append(f"        ... and {len(self.divergences) - 10} more")
        if orig is not None and orig.windowed and not orig.agreed:
            lines.append("    DIVERGED -- the owner's own script disagrees with the Python engine:")
            if orig.error:
                lines.append(f"        {orig.error}")
            for d in orig.divergences[:10]:
                lines.append(f"        {d.explain()}")
        for note in [*self.limitations, *(orig.notes if orig else [])]:
            lines.append(f"    note: {note}")
        return "\n".join(lines)

    @staticmethod
    def _original_lines(orig: OriginalCheck) -> list[str]:
        out = []
        if orig.original is not None:
            out.append("    " + orig.original.line() + f"  <- owner's script, most recent {orig.n_bars:,} bars")
        if orig.python is not None:
            out.append("    " + orig.python.line() + "  <- Python engine, the same bars")
        if orig.error:
            out.append(f"    {'original':<16} FAILED: {orig.error}")
        return out


def run_parity(
    spec: StrategySpec,
    bars,
    targets: tuple[str, ...] = ALL_TARGETS,
    original: OriginalScript | str | None = "auto",
) -> ParityResult:
    """Run every target over ``bars`` and compare.

    ``original="auto"`` looks for the owner's script in the spec's library entry
    (``library/<spec id>/original/manifest.yaml``); pass an
    :class:`OriginalScript` to use a specific one, or None to skip it.
    """
    instrument = get_instrument(bars.symbol)
    result = ParityResult(
        spec_id=spec.id,
        spec_hash=spec.hash,
        symbol=bars.symbol,
        timeframe=bars.timeframe,
        n_bars=len(bars),
    )

    python_fp = None
    if "python" in targets:
        try:
            sig = compile_to_python(spec).evaluate(bars, instrument)
            python_fp = fp.from_arrays("python", bars, sig.long_entry, sig.short_entry)
            result.fingerprints.append(python_fp)
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

    if "original" in targets and original is not None:
        script = original_for(spec.id) if original == "auto" else original
        if isinstance(script, OriginalScript):
            check = _run_original(spec, bars, script, instrument, python_fp)
            if check.windowed:
                result.original = check
            elif check.error:
                result.errors["original"] = check.error
            elif check.original is not None:
                result.fingerprints.append(check.original)
                result.limitations.extend(n for n in check.notes if n not in result.limitations)

    result.divergences = fp.compare(result.fingerprints, bars=bars)
    return result


def _run_original(spec, bars, script: OriginalScript, instrument, python_fp) -> OriginalCheck:
    window = script.window_bars
    windowed = bool(window) and len(bars) > int(window)
    sub = bars.slice(len(bars) - int(window), len(bars)) if windowed else bars
    check = OriginalCheck(script=script, n_bars=len(sub), windowed=windowed)
    try:
        series = run_pine(
            script.source, sub, [script.long, script.short],
            mintick=instrument.tick_size, inputs=script.inputs, notes=check.notes,
        )
        check.original = fp.from_arrays(
            "original", sub,
            np.asarray(series[script.long], dtype=bool),
            np.asarray(series[script.short], dtype=bool),
            meta={"sha256": script.sha256, "file": script.path.name},
        )
    except PineError as exc:
        check.error = f"interpreter does not implement: {exc}"
        return check
    except Exception as exc:
        check.error = str(exc)
        return check
    if windowed or python_fp is None:
        sig = compile_to_python(spec).evaluate(sub, instrument)
        check.python = fp.from_arrays("python", sub, sig.long_entry, sig.short_entry)
    else:
        check.python = python_fp
    if windowed:
        check.divergences = fp.compare([check.python, check.original], bars=sub)
    return check


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
