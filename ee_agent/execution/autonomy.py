"""The autonomy ladder and the kill-switch hierarchy (abilities 57, 61).

Five levels. Promotion is earned against recorded evidence; demotion on drift is
automatic and does not ask.

    1  watch      -- observes and comments. Places nothing.
    2  propose    -- proposes an order and waits for the client. Nothing auto-sends.
    3  paper      -- trades live in parallel on paper. Real orders still blocked.
    4  capped     -- trades live, with caps on size, daily loss and trade count.
    5  unattended -- trades live without caps. Kill switches still apply.

Kill switches exist at three levels -- strategy, account, global -- and any of
them tripping stops everything beneath it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path

from ee_agent.errors import AutonomyViolation, KillSwitchTripped
from ee_agent.paths import ee_home


class Level(IntEnum):
    WATCH = 1
    PROPOSE = 2
    PAPER = 3
    CAPPED = 4
    UNATTENDED = 5

    @property
    def label(self) -> str:
        return {
            1: "watch and comment",
            2: "propose and wait",
            3: "paper trade in parallel",
            4: "live with caps",
            5: "live unattended",
        }[int(self)]

    @property
    def places_real_orders(self) -> bool:
        return self >= Level.CAPPED


@dataclass
class Caps:
    """Applied at level 4. Level 5 removes them; kill switches remain."""

    max_contracts: float = 1.0
    max_trades_per_day: int = 6
    max_daily_loss_usd: float = 500.0
    max_open_positions: int = 1


@dataclass
class PromotionEvidence:
    """What a promotion has to be earned against. No level is granted on a date."""

    live_or_paper_sessions: int = 0
    trades: int = 0
    drift_flags: int = 0
    kill_switch_trips: int = 0
    realised_vs_backtest_ratio: float = 0.0  # 1.0 = live matches the backtest
    days_running: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


PROMOTION_REQUIREMENTS: dict[int, dict] = {
    Level.PROPOSE: {"live_or_paper_sessions": 1, "trades": 0, "max_drift_flags": 99},
    Level.PAPER: {"live_or_paper_sessions": 3, "trades": 5, "max_drift_flags": 5},
    Level.CAPPED: {"live_or_paper_sessions": 20, "trades": 30, "max_drift_flags": 2},
    Level.UNATTENDED: {"live_or_paper_sessions": 60, "trades": 150, "max_drift_flags": 0},
}


@dataclass
class KillSwitchConfig:
    daily_loss_usd: float = 1000.0
    consecutive_losses: int = 4
    max_drift_sigma: float = 3.0
    max_latency_ms: float = 2000.0
    max_data_staleness_s: float = 90.0
    max_api_errors: int = 5


@dataclass
class KillSwitchState:
    scope: str  # strategy | account | global
    name: str
    tripped: bool = False
    reason: str = ""
    tripped_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class KillSwitches:
    """Three levels. Global covers everything; account covers its strategies."""

    def __init__(self, config: KillSwitchConfig | None = None, path: Path | None = None):
        self.config = config or KillSwitchConfig()
        self.path = path or (ee_home() / "kill-switches.jsonl")
        self.states: dict[tuple[str, str], KillSwitchState] = {}

    def _state(self, scope: str, name: str) -> KillSwitchState:
        key = (scope, name)
        if key not in self.states:
            self.states[key] = KillSwitchState(scope=scope, name=name)
        return self.states[key]

    def trip(self, scope: str, name: str, reason: str) -> KillSwitchState:
        state = self._state(scope, name)
        if not state.tripped:
            state.tripped = True
            state.reason = reason
            state.tripped_at = datetime.now(timezone.utc).isoformat()
            self._log(state)
        return state

    def reset(self, scope: str, name: str) -> None:
        """Only a human resets a kill switch. Nothing in the agent calls this."""
        state = self._state(scope, name)
        state.tripped, state.reason, state.tripped_at = False, "", ""
        self._log(state)

    def is_tripped(self, strategy_id: str = "", account: str = "") -> KillSwitchState | None:
        for key in (("global", "global"), ("account", account), ("strategy", strategy_id)):
            if not key[1]:
                continue
            state = self.states.get(key)
            if state and state.tripped:
                return state
        return None

    def check(self, strategy_id: str = "", account: str = "") -> None:
        state = self.is_tripped(strategy_id=strategy_id, account=account)
        if state:
            raise KillSwitchTripped(
                f"{state.scope} kill switch '{state.name}' is tripped: {state.reason} "
                f"(at {state.tripped_at}). A human must reset it."
            )

    # ------------------------------------------------------------- triggers
    def evaluate(
        self,
        *,
        strategy_id: str = "",
        account: str = "",
        daily_pnl: float = 0.0,
        consecutive_losses: int = 0,
        drift_sigma: float = 0.0,
        latency_ms: float = 0.0,
        data_staleness_s: float = 0.0,
        api_errors: int = 0,
    ) -> list[KillSwitchState]:
        """Every trigger the spec names: daily loss, consecutive losses, drift,
        latency, data staleness, API errors."""
        tripped: list[KillSwitchState] = []
        c = self.config
        if daily_pnl <= -abs(c.daily_loss_usd):
            tripped.append(
                self.trip("account", account or "default",
                          f"daily loss {daily_pnl:,.2f} breached the {c.daily_loss_usd:,.2f} limit")
            )
        if consecutive_losses >= c.consecutive_losses:
            tripped.append(
                self.trip("strategy", strategy_id or "default",
                          f"{consecutive_losses} consecutive losses (limit {c.consecutive_losses})")
            )
        if drift_sigma >= c.max_drift_sigma:
            tripped.append(
                self.trip("strategy", strategy_id or "default",
                          f"live fills are {drift_sigma:.1f} sigma from the backtest distribution")
            )
        if latency_ms >= c.max_latency_ms:
            tripped.append(
                self.trip("global", "global", f"order latency {latency_ms:.0f}ms exceeds {c.max_latency_ms:.0f}ms")
            )
        if data_staleness_s >= c.max_data_staleness_s:
            tripped.append(
                self.trip("global", "global",
                          f"market data is {data_staleness_s:.0f}s stale (limit {c.max_data_staleness_s:.0f}s)")
            )
        if api_errors >= c.max_api_errors:
            tripped.append(
                self.trip("account", account or "default", f"{api_errors} consecutive API errors")
            )
        return tripped

    def _log(self, state: KillSwitchState) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **state.to_dict()}) + "\n")
        except OSError:
            pass

    def summary(self) -> str:
        active = [s for s in self.states.values() if s.tripped]
        if not active:
            return "[kill switches] all clear."
        return "[kill switches] TRIPPED:\n" + "\n".join(
            f"    {s.scope}:{s.name} -- {s.reason} ({s.tripped_at})" for s in active
        )


@dataclass
class AutonomyLadder:
    strategy_id: str
    level: Level = Level.WATCH
    caps: Caps = field(default_factory=Caps)
    evidence: PromotionEvidence = field(default_factory=PromotionEvidence)
    history: list[dict] = field(default_factory=list)
    path: Path | None = None

    # ------------------------------------------------------------ authority
    def authorize(self, intent, *, is_paper: bool = False) -> None:
        """Raises unless this level is allowed to send this order for real."""
        if is_paper:
            return
        if not self.level.places_real_orders:
            raise AutonomyViolation(
                f"{self.strategy_id} is at autonomy level {int(self.level)} ({self.level.label}). "
                f"It may not place real orders. Promote it deliberately, or run it on paper."
            )
        if self.level == Level.CAPPED:
            if abs(intent.size) > self.caps.max_contracts:
                raise AutonomyViolation(
                    f"level 4 caps size at {self.caps.max_contracts}; this order is {intent.size}."
                )

    def check_daily(self, trades_today: int, daily_pnl: float) -> None:
        if self.level != Level.CAPPED:
            return
        if trades_today >= self.caps.max_trades_per_day:
            raise AutonomyViolation(
                f"level 4 caps trades at {self.caps.max_trades_per_day}/day; {trades_today} already taken."
            )
        if daily_pnl <= -abs(self.caps.max_daily_loss_usd):
            raise AutonomyViolation(
                f"level 4 caps daily loss at {self.caps.max_daily_loss_usd:,.2f}; "
                f"today is {daily_pnl:,.2f}. Stopped for the day."
            )

    # ---------------------------------------------------------- progression
    def can_promote(self) -> tuple[bool, str]:
        if self.level >= Level.UNATTENDED:
            return False, "already at the top of the ladder"
        target = Level(int(self.level) + 1)
        req = PROMOTION_REQUIREMENTS[target]
        e = self.evidence
        missing = []
        if e.live_or_paper_sessions < req["live_or_paper_sessions"]:
            missing.append(
                f"{req['live_or_paper_sessions'] - e.live_or_paper_sessions} more session(s)"
            )
        if e.trades < req["trades"]:
            missing.append(f"{req['trades'] - e.trades} more trade(s)")
        if e.drift_flags > req["max_drift_flags"]:
            missing.append(
                f"drift flags must be at most {req['max_drift_flags']} (currently {e.drift_flags})"
            )
        if e.kill_switch_trips > 0 and target >= Level.CAPPED:
            missing.append(f"{e.kill_switch_trips} kill-switch trip(s) on record")
        if missing:
            return False, f"not yet promotable to level {int(target)}: " + ", ".join(missing)
        return True, f"eligible for level {int(target)} ({target.label})"

    def promote(self, approved_by: str) -> Level:
        ok, why = self.can_promote()
        if not ok:
            raise AutonomyViolation(why)
        if not approved_by:
            raise AutonomyViolation("promotion requires a named approver; it is never automatic")
        self._set(Level(int(self.level) + 1), f"promoted by {approved_by}: {why}")
        return self.level

    def demote(self, reason: str) -> Level:
        """Automatic on drift. Never asks, never waits."""
        if self.level > Level.WATCH:
            self._set(Level(int(self.level) - 1), f"AUTOMATIC DEMOTION: {reason}")
        return self.level

    def _set(self, level: Level, reason: str) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "strategy_id": self.strategy_id,
            "from": int(self.level),
            "to": int(level),
            "reason": reason,
            "evidence": self.evidence.to_dict(),
        }
        self.level = level
        self.history.append(record)
        path = self.path or (ee_home() / "autonomy.jsonl")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            pass

    def status(self) -> str:
        ok, why = self.can_promote()
        return (
            f"[autonomy] {self.strategy_id}: level {int(self.level)} -- {self.level.label}\n"
            f"           {'ELIGIBLE: ' if ok else ''}{why}\n"
            f"           evidence: {self.evidence.to_dict()}"
        )
