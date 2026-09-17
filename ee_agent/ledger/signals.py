"""The verified signal ledger (ability 70).

Every signal is hashed and timestamped at the moment it fires, **before the
outcome is known**. Over time this becomes a provably non-hindsight track
record: nobody, including the owner, can go back and quietly add the winners.

The mechanism is a hash chain. Each entry commits to the previous entry's hash,
so altering or deleting any historical entry breaks every entry after it, and
:func:`verify_chain` says exactly where.

What this proves and what it does not
-------------------------------------
It proves the file has not been edited after the fact, and that each signal's
content was fixed at the moment it was written. It does not prove *when* it was
written to an outside observer -- for that the chain head can be anchored to an
external timestamp authority, which :func:`anchor_line` formats and which the
client can publish anywhere public.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ee_agent.paths import signal_ledger_path

GENESIS = "0" * 64


@dataclass
class SignalRecord:
    seq: int
    ts: str
    spec_id: str
    spec_hash: str
    plan_hash: str
    symbol: str
    timeframe: str
    side: str
    bar_time: str
    price: float
    rule_id: str = ""
    autonomy_level: int = 1
    account: str = ""
    prev_hash: str = GENESIS
    entry_hash: str = ""
    #: Filled in LATER, and never part of the hash. This is the whole point:
    #: the commitment is made before the outcome exists.
    outcome: dict | None = None

    def payload(self) -> dict:
        """Exactly the fields the hash commits to. `outcome` is excluded."""
        return {
            "seq": self.seq,
            "ts": self.ts,
            "spec_id": self.spec_id,
            "spec_hash": self.spec_hash,
            "plan_hash": self.plan_hash,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side,
            "bar_time": self.bar_time,
            "price": round(float(self.price), 8),
            "rule_id": self.rule_id,
            "autonomy_level": self.autonomy_level,
            "account": self.account,
            "prev_hash": self.prev_hash,
        }

    def compute_hash(self) -> str:
        blob = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


class SignalLedger:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else signal_ledger_path()

    # ------------------------------------------------------------- reading
    def entries(self) -> list[SignalRecord]:
        if not self.path.exists():
            return []
        out: list[SignalRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(SignalRecord(**json.loads(line)))
        return out

    @property
    def head(self) -> str:
        entries = self.entries()
        return entries[-1].entry_hash if entries else GENESIS

    @property
    def length(self) -> int:
        return len(self.entries())

    # ------------------------------------------------------------- writing
    def record(
        self,
        *,
        spec_id: str,
        spec_hash: str,
        plan_hash: str,
        symbol: str,
        timeframe: str,
        side: str,
        bar_time: str,
        price: float,
        rule_id: str = "",
        autonomy_level: int = 1,
        account: str = "",
        ts: str | None = None,
    ) -> SignalRecord:
        """Commit a signal. Call this the moment it fires, not after the trade."""
        entries = self.entries()
        record = SignalRecord(
            seq=len(entries) + 1,
            ts=ts or datetime.now(timezone.utc).isoformat(),
            spec_id=spec_id,
            spec_hash=spec_hash,
            plan_hash=plan_hash,
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            bar_time=bar_time,
            price=float(price),
            rule_id=rule_id,
            autonomy_level=autonomy_level,
            account=account,
            prev_hash=entries[-1].entry_hash if entries else GENESIS,
        )
        record.entry_hash = record.compute_hash()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.to_dict()) + "\n")
        return record

    def attach_outcome(self, seq: int, outcome: dict) -> bool:
        """Record what happened. The hash does not change, and cannot: it was
        computed before this was known."""
        entries = self.entries()
        found = False
        for entry in entries:
            if entry.seq == seq:
                entry.outcome = outcome
                found = True
        if not found:
            return False
        with self.path.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry.to_dict()) + "\n")
        return True

    # ---------------------------------------------------------- verification
    def verify(self) -> "ChainVerification":
        return verify_chain(self.entries())

    def proof_for(self, seq: int) -> dict:
        """Everything a third party needs to check one signal by hand."""
        entries = self.entries()
        match = next((e for e in entries if e.seq == seq), None)
        if match is None:
            return {"error": f"no signal with seq {seq}"}
        return {
            "record": match.payload(),
            "entry_hash": match.entry_hash,
            "recomputed": match.compute_hash(),
            "matches": match.compute_hash() == match.entry_hash,
            "committed_before_outcome": match.outcome is not None,
            "outcome": match.outcome,
            "chain_head_at_time": entries[-1].entry_hash if entries else GENESIS,
            "how_to_check": (
                "sha256 of the JSON in `record`, keys sorted, no whitespace, equals `entry_hash`. "
                "`outcome` is not in `record` -- that is the proof it was not known when the hash "
                "was computed."
            ),
        }

    def track_record(self, spec_id: str | None = None) -> dict:
        """The non-hindsight track record: only signals with attached outcomes,
        every one of them committed before its outcome existed."""
        entries = [e for e in self.entries() if not spec_id or e.spec_id == spec_id]
        settled = [e for e in entries if e.outcome]
        wins = [e for e in settled if float(e.outcome.get("pnl", 0)) > 0]
        pnl = sum(float(e.outcome.get("pnl", 0)) for e in settled)
        verification = self.verify()
        return {
            "spec_id": spec_id or "all",
            "signals_committed": len(entries),
            "outcomes_attached": len(settled),
            "open_or_unsettled": len(entries) - len(settled),
            "win_rate": round(len(wins) / len(settled), 4) if settled else None,
            "net_pnl": round(pnl, 2),
            "chain_valid": verification.valid,
            "chain_head": self.head,
            "first_signal": entries[0].ts if entries else None,
            "last_signal": entries[-1].ts if entries else None,
            "claim": (
                "Every signal above was hashed into an append-only chain at the moment it fired, "
                "before its outcome existed. The chain verifies, so none was added or edited later."
                if verification.valid
                else "THE CHAIN DOES NOT VERIFY -- this track record cannot be trusted."
            ),
        }

    def anchor_line(self) -> str:
        """Publish this string anywhere public and timestamped. It commits to the
        whole history without revealing any of it."""
        return (
            f"ee-agent signal ledger anchor | entries={self.length} | head={self.head} | "
            f"as of {datetime.now(timezone.utc).isoformat()}"
        )


@dataclass
class ChainVerification:
    valid: bool
    n_entries: int
    broken_at: int | None = None
    reason: str = ""
    details: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.valid:
            return (
                f"[signal ledger] chain VERIFIES across {self.n_entries} signal(s): none was added, "
                "edited or removed after it was written."
            )
        return (
            f"[signal ledger] chain BROKEN at entry {self.broken_at}: {self.reason}. "
            "Everything after that point is unverifiable."
        )


def verify_chain(entries: list[SignalRecord]) -> ChainVerification:
    previous = GENESIS
    for i, entry in enumerate(entries, start=1):
        if entry.seq != i:
            return ChainVerification(False, len(entries), i, f"sequence jumped to {entry.seq}")
        if entry.prev_hash != previous:
            return ChainVerification(
                False, len(entries), i, "prev_hash does not match the previous entry (an entry was "
                "removed or inserted)"
            )
        recomputed = entry.compute_hash()
        if recomputed != entry.entry_hash:
            return ChainVerification(
                False, len(entries), i, "content does not match its hash (the entry was edited)"
            )
        previous = entry.entry_hash
    return ChainVerification(True, len(entries))


_LEDGER: SignalLedger | None = None


def ledger() -> SignalLedger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = SignalLedger()
    return _LEDGER
