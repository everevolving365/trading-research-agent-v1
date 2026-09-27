"""The founder's library (Section 11, abilities 77-80).

Positioning, which the code enforces rather than merely documents: the default
path is always the client describing their own strategy. The library is offered
ONCE, plainly, with no default selection and no upsell -- :func:`offer_text`
returns a single sentence and :func:`should_offer` returns False forever after
the first time.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ee_agent.paths import ee_home, library_dir
from ee_agent.spec.model import StrategySpec

OFFER_MARKER = "library-offered.json"


@dataclass
class LibraryEntry:
    id: str
    name: str
    path: Path
    has_spec: bool
    has_writeup: bool
    original_files: list[str] = field(default_factory=list)
    description: str = ""
    placeholder: bool = False

    def load_spec(self) -> StrategySpec:
        if not self.has_spec:
            raise FileNotFoundError(
                f"library entry '{self.id}' has no spec.yaml. It is a placeholder until the owner "
                "supplies the strategy (BLOCKERS.md)."
            )
        return StrategySpec.load(self.path / "spec.yaml")

    def writeup(self) -> str:
        path = self.path / "how-i-trade-it.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def line(self) -> str:
        mark = " (placeholder -- not yet supplied)" if self.placeholder else ""
        return f"  {self.id:<22} {self.name}{mark}"


def entries(root: Path | None = None) -> list[LibraryEntry]:
    directory = Path(root or library_dir())
    if not directory.exists():
        return []
    out: list[LibraryEntry] = []
    for child in sorted(p for p in directory.iterdir() if p.is_dir()):
        spec_path = child / "spec.yaml"
        has_spec = spec_path.exists()
        name = child.name
        description = ""
        placeholder = False
        if has_spec:
            try:
                raw = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
                name = raw.get("name", child.name)
                description = (raw.get("notes") or "").strip().splitlines()[0] if raw.get("notes") else ""
                placeholder = bool(raw.get("placeholder"))
            except Exception:
                has_spec = False
        originals = sorted(p.name for p in (child / "original").glob("*.pine")) if (child / "original").exists() else []
        out.append(
            LibraryEntry(
                id=child.name,
                name=name,
                path=child,
                has_spec=has_spec,
                has_writeup=(child / "how-i-trade-it.md").exists(),
                original_files=originals,
                description=description,
                placeholder=placeholder or not has_spec,
            )
        )
    return out


def get(entry_id: str, root: Path | None = None) -> LibraryEntry:
    for entry in entries(root):
        if entry.id == entry_id:
            return entry
    raise KeyError(f"No library entry '{entry_id}'. Available: {', '.join(e.id for e in entries(root))}")


def _marker_path() -> Path:
    return ee_home() / OFFER_MARKER


def should_offer() -> bool:
    """Offered once, never pushed. After the first time this is False forever."""
    return not _marker_path().exists()


def mark_offered() -> None:
    path = _marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"offered": True}), encoding="utf-8")


def offer_text() -> str:
    """One sentence. No push, no default selection, no upsell (Section 11)."""
    available = [e for e in entries() if e.has_spec and not e.placeholder]
    if not available:
        return ""
    return (
        "You can also start from one of Arinze's strategies if you'd rather not build from scratch: "
        + ", ".join(e.id for e in available)
        + ". Your own strategy is the default -- I will not mention this again."
    )


def catalogue() -> str:
    found = entries()
    if not found:
        return "[library] empty."
    lines = [f"[library] {len(found)} entry(ies). Your own strategy is always the default path."]
    lines += [e.line() for e in found]
    for entry in found:
        if entry.has_spec and not entry.has_writeup:
            lines.append(f"    {entry.id}: no how-i-trade-it.md yet (owner to supply)")
        if entry.has_spec and not entry.original_files:
            lines.append(f"    {entry.id}: no original .pine files for regression comparison (owner to supply)")
    return "\n".join(lines)


def signal_count_delta(entry_id: str, bars, root: Path | None = None) -> dict:
    """Section 11: the signal-count delta between the generated scripts and the
    owner's original, over the same bars.

    Where the entry carries its original with a manifest, the original is run
    unmodified through the Pine interpreter and compared bar for bar. Without
    one, that is stated rather than silently skipped.
    """
    entry = get(entry_id, root)
    from ee_agent.parity.original import load_original

    script = load_original(entry.path)
    if script is None:
        return {
            "entry": entry_id,
            "available": False,
            "original_files": entry.original_files,
            "note": (
                "This entry has no original script with a manifest, so there is nothing to compare "
                "against. The generated scripts are still verified against the Python engine and "
                "the live config by the parity harness."
            ),
        }
    from ee_agent.parity.harness import run_parity

    parity = run_parity(entry.load_spec(), bars, original=script)
    generated = next((f for f in parity.fingerprints if f.target == "pine_indicator"), None)
    if parity.original is not None:  # ran over a trailing window
        orig_fp, same_bars_fp = parity.original.original, parity.original.python
        n_bars, divergences = parity.original.n_bars, len(parity.original.divergences)
        error = parity.original.error
    else:
        orig_fp = next((f for f in parity.fingerprints if f.target == "original"), None)
        same_bars_fp, n_bars = generated, len(bars)
        divergences = len(parity.divergences)
        error = parity.errors.get("original", "")
    original_signals = len(orig_fp.signals) if orig_fp else 0
    generated_signals = len(same_bars_fp.signals) if same_bars_fp else 0
    return {
        "entry": entry_id,
        "available": True,
        "original_files": entry.original_files,
        "compared_bars": n_bars,
        "original_signals": original_signals,
        "generated_signals": generated_signals,
        "delta": generated_signals - original_signals,
        "divergences": divergences,
        "agreed": parity.agreed,
        "error": error,
        "note": (
            f"The owner's original ({script.path.name}) was run unmodified over {n_bars:,} bars: "
            f"{original_signals} signal(s) against {generated_signals} from the generated strategy, "
            f"{divergences} bar(s) of disagreement."
        ),
    }
