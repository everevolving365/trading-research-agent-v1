"""The global position ledger -- one ledger, every account, every position.

Hard rule 3, stated in full because it is the rule with a real-world consequence
attached: the owner has been formally warned by a prop firm for cross-account
hedging that happened without his knowledge when two bots held opposing
positions. A repeat means account removal.

So: any order that would create opposing exposure across accounts is refused
**at the order layer, before it reaches any API**, correlation-aware rather than
symbol-matched. Long NQ on one account and short ES on another is a hedge.

There is no override flag. There is no force parameter. A caller that wants to
bypass this would have to edit this file, and the test suite would fail.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from ee_agent.errors import HedgeRefused
from ee_agent.instruments.registry import registry
from ee_agent.paths import ee_home


@dataclass
class Position:
    account: str
    symbol: str
    size: float  # signed: positive long, negative short
    avg_price: float
    strategy_id: str = ""
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def side(self) -> str:
        return "long" if self.size > 0 else ("short" if self.size < 0 else "flat")

    @property
    def flat(self) -> bool:
        return abs(self.size) < 1e-9

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OrderIntent:
    account: str
    symbol: str
    side: str  # buy | sell
    size: float
    order_type: str = "market"
    price: float | None = None
    strategy_id: str = ""
    reason: str = ""

    @property
    def signed_size(self) -> float:
        return self.size if self.side.lower() in ("buy", "long") else -self.size

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HedgeCheck:
    allowed: bool
    reason: str = ""
    conflicts: list[dict] = field(default_factory=list)

    def raise_if_refused(self) -> None:
        if not self.allowed:
            raise HedgeRefused(self.reason)


class PositionLedger:
    """Thread-safe, append-logged, and the only thing allowed to say yes."""

    def __init__(self, path: Path | None = None, correlation_threshold: float | None = None):
        self.path = path or (ee_home() / "position-ledger.jsonl")
        self.positions: dict[tuple[str, str], Position] = {}
        self._lock = threading.RLock()
        self.correlation_threshold = (
            correlation_threshold if correlation_threshold is not None else registry().hedge_threshold
        )
        self.refusals: list[dict] = []

    # ------------------------------------------------------------- reading
    def get(self, account: str, symbol: str) -> Position | None:
        return self.positions.get((account, symbol.upper()))

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if not p.flat]

    def net_exposure(self, symbol: str) -> float:
        return sum(p.size for p in self.open_positions() if p.symbol == symbol.upper())

    def by_account(self, account: str) -> list[Position]:
        return [p for p in self.open_positions() if p.account == account]

    # -------------------------------------------------------- hedge refusal
    def check(self, intent: OrderIntent) -> HedgeCheck:
        """Would this order create opposing exposure anywhere in the system?

        Evaluated against the position the order would *result in*, not the
        order itself: reducing or closing a position is always allowed, even
        when an opposing position exists elsewhere -- refusing that would trap
        the client in a hedge they are trying to exit.
        """
        with self._lock:
            reg = registry()
            symbol = intent.symbol.upper()
            existing = self.get(intent.account, symbol)
            current = existing.size if existing else 0.0
            resulting = current + intent.signed_size

            if abs(resulting) <= abs(current) + 1e-9 and (
                current == 0 or (resulting * current >= 0)
            ):
                # reducing or flattening: never a new hedge
                if abs(resulting) < abs(current) or abs(resulting) < 1e-9:
                    return HedgeCheck(True, "reduces existing exposure")

            if abs(resulting) < 1e-9:
                return HedgeCheck(True, "flattens the position")

            resulting_side = 1.0 if resulting > 0 else -1.0
            conflicts: list[dict] = []
            for pos in self.open_positions():
                if pos.account == intent.account and pos.symbol == symbol:
                    continue
                try:
                    correlation = reg.correlation(symbol, pos.symbol)
                except KeyError:
                    correlation = 1.0 if pos.symbol == symbol else 0.0
                pos_side = 1.0 if pos.size > 0 else -1.0
                # Positive correlation + opposite sides = hedge.
                # Negative correlation + same sides = also a hedge.
                effective = pos_side * correlation
                if resulting_side * effective < 0 and abs(correlation) >= self.correlation_threshold:
                    conflicts.append(
                        {
                            "account": pos.account,
                            "symbol": pos.symbol,
                            "size": pos.size,
                            "side": pos.side,
                            "correlation": round(correlation, 3),
                            "strategy_id": pos.strategy_id,
                        }
                    )

            if conflicts:
                lines = [
                    f"REFUSED at the order layer: {intent.side} {intent.size} {symbol} on "
                    f"{intent.account} would create opposing exposure across accounts.",
                ]
                for c in conflicts:
                    same = "the same instrument" if c["symbol"] == symbol else (
                        f"{c['symbol']}, correlation {c['correlation']}"
                    )
                    lines.append(
                        f"    {c['account']} is already {c['side']} {abs(c['size'])} {c['symbol']} "
                        f"({same})"
                    )
                lines.append(
                    "    No order was sent to any API. Flatten the conflicting position first. "
                    "(Hard rule 3: cross-account hedging is what got the account a formal warning.)"
                )
                check = HedgeCheck(False, "\n".join(lines), conflicts)
                self._log_refusal(intent, check)
                return check

            return HedgeCheck(True, "no opposing exposure")

    # -------------------------------------------------------------- writing
    def apply_fill(
        self, account: str, symbol: str, signed_size: float, price: float, strategy_id: str = ""
    ) -> Position:
        """Record a fill. Callers must have passed :meth:`check` first --
        :class:`ee_agent.execution.router.OrderRouter` enforces that."""
        with self._lock:
            key = (account, symbol.upper())
            pos = self.positions.get(key)
            if pos is None:
                pos = Position(account=account, symbol=symbol.upper(), size=0.0, avg_price=price,
                               strategy_id=strategy_id)
                self.positions[key] = pos
            new_size = pos.size + signed_size
            if pos.size == 0 or (pos.size > 0) == (signed_size > 0):
                total = abs(pos.size) + abs(signed_size)
                pos.avg_price = (
                    (pos.avg_price * abs(pos.size) + price * abs(signed_size)) / total if total else price
                )
            elif abs(signed_size) > abs(pos.size):
                pos.avg_price = price  # flipped through flat
            pos.size = 0.0 if abs(new_size) < 1e-9 else new_size
            pos.updated_at = datetime.now(timezone.utc).isoformat()
            if strategy_id:
                pos.strategy_id = strategy_id
            self._append(
                {
                    "event": "fill",
                    "ts": pos.updated_at,
                    "account": account,
                    "symbol": symbol.upper(),
                    "signed_size": signed_size,
                    "price": price,
                    "resulting_size": pos.size,
                    "strategy_id": strategy_id,
                }
            )
            return pos

    def flatten(self, account: str, symbol: str) -> None:
        with self._lock:
            pos = self.get(account, symbol)
            if pos:
                pos.size = 0.0
                pos.updated_at = datetime.now(timezone.utc).isoformat()
                self._append(
                    {"event": "flatten", "ts": pos.updated_at, "account": account, "symbol": symbol.upper()}
                )

    def _log_refusal(self, intent: OrderIntent, check: HedgeCheck) -> None:
        record = {
            "event": "hedge_refused",
            "ts": datetime.now(timezone.utc).isoformat(),
            "intent": intent.to_dict(),
            "conflicts": check.conflicts,
        }
        self.refusals.append(record)
        self._append(record)

    def _append(self, record: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:  # never let bookkeeping stop a flatten
            pass

    # ------------------------------------------------------------ reporting
    def summary(self) -> str:
        open_positions = self.open_positions()
        if not open_positions:
            return "[ledger] flat everywhere: no open positions on any account."
        lines = [f"[ledger] {len(open_positions)} open position(s) across all accounts:"]
        for p in sorted(open_positions, key=lambda x: (x.account, x.symbol)):
            lines.append(
                f"    {p.account:<18} {p.symbol:<8} {p.side:<5} {abs(p.size):>6.2f} @ {p.avg_price:,.4f}"
                f"   {p.strategy_id}"
            )
        if self.refusals:
            lines.append(f"    {len(self.refusals)} order(s) refused for cross-account hedging this session.")
        return "\n".join(lines)


_LEDGER: PositionLedger | None = None


def ledger() -> PositionLedger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = PositionLedger()
    return _LEDGER


def set_ledger(new: PositionLedger) -> None:
    global _LEDGER
    _LEDGER = new
