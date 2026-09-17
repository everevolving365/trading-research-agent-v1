"""Signal fingerprints -- the proof object for Law Two.

A fingerprint is the ordered list of every signal a target produced: bar
timestamp, side, and rule. Hashed, it is one short string that answers "are
these the same strategy?" for any two targets.

Fingerprints are compared, never trusted blindly: when two differ, the harness
reports the divergence *to the bar*, because "they disagree" is not an answer a
client can act on.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Signal:
    ts: str
    side: str
    rule: str = ""

    def key(self) -> str:
        return f"{self.ts}|{self.side}"


@dataclass
class Fingerprint:
    target: str
    symbol: str
    timeframe: str
    signals: list[Signal] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def digest(self) -> str:
        h = hashlib.sha256()
        for s in self.signals:
            h.update(s.key().encode("utf-8"))
        return "sha256:" + h.hexdigest()

    @property
    def short(self) -> str:
        return self.digest.split(":")[1][:12]

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.signals:
            out[s.side] = out.get(s.side, 0) + 1
        return out

    def keys(self) -> set[str]:
        return {s.key() for s in self.signals}

    def line(self) -> str:
        return (
            f"{self.target:<16} {self.short}  {len(self.signals):>6} signals  "
            f"{self.counts}"
        )


def from_arrays(
    target: str, bars, long_signal: np.ndarray, short_signal: np.ndarray, meta: dict | None = None
) -> Fingerprint:
    signals: list[Signal] = []
    ts = bars.df["ts"]
    for i in range(len(bars)):
        if long_signal[i]:
            signals.append(Signal(ts.iloc[i].isoformat(), "long"))
        if short_signal[i]:
            signals.append(Signal(ts.iloc[i].isoformat(), "short"))
    return Fingerprint(
        target=target, symbol=bars.symbol, timeframe=bars.timeframe, signals=signals, meta=meta or {}
    )


@dataclass
class Divergence:
    ts: str
    side: str
    present_in: list[str]
    absent_from: list[str]
    bar_index: int = -1
    context: dict = field(default_factory=dict)

    def explain(self) -> str:
        return (
            f"bar {self.bar_index} {self.ts} {self.side}: fired in "
            f"{', '.join(self.present_in)}; did not fire in {', '.join(self.absent_from)}"
            + (f"  ({self.context})" if self.context else "")
        )


def compare(fingerprints: list[Fingerprint], bars=None, limit: int = 50) -> list[Divergence]:
    """Locate every divergence to the bar, not just report that one exists."""
    if len(fingerprints) < 2:
        return []
    index: dict[str, int] = {}
    if bars is not None:
        index = {bars.df["ts"].iloc[i].isoformat(): i for i in range(len(bars))}

    all_keys: set[str] = set()
    per_target: dict[str, set[str]] = {}
    for fp in fingerprints:
        keys = fp.keys()
        per_target[fp.target] = keys
        all_keys |= keys

    out: list[Divergence] = []
    for key in sorted(all_keys):
        present = [t for t, keys in per_target.items() if key in keys]
        if len(present) == len(fingerprints):
            continue
        absent = [t for t in per_target if t not in present]
        ts, side = key.split("|")
        out.append(
            Divergence(
                ts=ts, side=side, present_in=present, absent_from=absent, bar_index=index.get(ts, -1)
            )
        )
        if len(out) >= limit:
            break
    return out
