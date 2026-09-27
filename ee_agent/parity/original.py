"""The fifth parity target: the owner's own TradingView script, run verbatim.

A founder's-library entry can carry the script it was ported from in
``original/``, with a ``manifest.yaml`` saying which variables are its long and
short signals. The parity harness then runs that script -- unmodified, through
the same Pine interpreter -- and compares it with the Python engine bar for bar.

That closes the last gap in "the strategy you described and the strategy trading
your account are provably the same object": for a ported strategy, what the
owner described IS a script, and this is the proof the port is faithful.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ee_agent.paths import library_dir

_CRLF = b"\r\n"
_LF = b"\n"


@dataclass
class OriginalScript:
    entry_id: str
    path: Path
    source: str
    long: str
    short: str
    inputs: dict = field(default_factory=dict)
    window_bars: int | None = None
    url: str = ""

    @property
    def sha256(self) -> str:
        return _digest(self.path.read_bytes())

    @property
    def label(self) -> str:
        return f"{self.path.name} (the owner's own script, unmodified)"


def _digest(raw: bytes) -> str:
    """SHA-256 of the script with LF line endings -- what TradingView serves.
    A Windows checkout may turn LF into CRLF; that is not a change to the script."""
    return hashlib.sha256(raw.replace(_CRLF, _LF)).hexdigest()


def load_original(entry_dir: Path) -> OriginalScript | None:
    """The original script of a library entry, or None if it has none."""
    manifest = Path(entry_dir) / "original" / "manifest.yaml"
    if not manifest.exists():
        return None
    meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    path = manifest.parent / str(meta["file"])
    raw = path.read_bytes()
    source = raw.replace(_CRLF, _LF).decode("utf-8")
    expected = str(meta.get("sha256") or "")
    if expected and not _digest(raw).startswith(expected):
        raise ValueError(
            f"{path.name} does not match the checksum in its manifest ({expected}). "
            "The original must be byte-for-byte the published script."
        )
    window = meta.get("window_bars")
    return OriginalScript(
        entry_id=Path(entry_dir).name,
        path=path,
        source=source,
        long=str(meta.get("long", "longSignal")),
        short=str(meta.get("short", "shortSignal")),
        inputs=dict(meta.get("inputs") or {}),
        window_bars=int(window) if window else None,
        url=str(meta.get("source") or ""),
    )


def original_for(spec_id: str, root: Path | None = None) -> OriginalScript | None:
    entry = Path(root or library_dir()) / spec_id
    return load_original(entry) if entry.exists() else None
