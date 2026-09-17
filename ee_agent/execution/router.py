"""The order router: the single door every order goes through.

Nothing reaches a broker adapter except through :meth:`OrderRouter.submit`, and
submit runs, in this order:

1. kill switches (strategy, account, global)
2. autonomy level and its caps
3. prop firm position limits
4. the global position ledger's hedge check   <- refuses before any API call
5. the adapter

Steps 1-4 happen with no network involved. An order refused at step 4 has not
touched an API, which is the property hard rule 3 actually requires.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from ee_agent.errors import AutonomyViolation, HedgeRefused, KillSwitchTripped
from ee_agent.execution.adapters import BrokerAdapter, BrokerFill, OrderAck
from ee_agent.execution.autonomy import AutonomyLadder, KillSwitches, Level
from ee_agent.execution.ledger import OrderIntent, PositionLedger, ledger as global_ledger
from ee_agent.instruments.registry import get_instrument
from ee_agent.prop.rules import position_limit_ok


@dataclass
class SubmitResult:
    accepted: bool
    stage: str  # kill_switch | autonomy | prop_limit | hedge | broker | paper
    ack: OrderAck | None = None
    message: str = ""
    intent: OrderIntent | None = None
    reached_api: bool = False

    def line(self) -> str:
        verdict = "ACCEPTED" if self.accepted else "REFUSED"
        api = "reached the broker" if self.reached_api else "no API call made"
        return f"[order] {verdict} at {self.stage} ({api}): {self.message}"


@dataclass
class DayState:
    day: str = ""
    trades: int = 0
    pnl: float = 0.0
    consecutive_losses: int = 0
    api_errors: int = 0


class OrderRouter:
    def __init__(
        self,
        adapter: BrokerAdapter,
        ladder: AutonomyLadder,
        position_ledger: PositionLedger | None = None,
        kill_switches: KillSwitches | None = None,
        prop_firm: str | None = None,
        prop_account: str | None = None,
        account_id: str = "default",
        clock: Callable[[], str] | None = None,
        sink: Callable[[str], None] = print,
    ):
        self.adapter = adapter
        self.ladder = ladder
        self.positions = position_ledger or global_ledger()
        self.kill = kill_switches or KillSwitches()
        self.prop_firm = prop_firm
        self.prop_account = prop_account
        self.account_id = account_id
        #: Returns the current trading day. Injectable so the replay harness can
        #: advance the calendar without waiting for real market time.
        self.clock = clock or (lambda: date.today().isoformat())
        self.sink = sink
        self.day = DayState(day=self.clock())
        self.shadow_fills: list[BrokerFill] = []
        self.submitted: list[SubmitResult] = []

    # ------------------------------------------------------------- submit
    def submit(self, intent: OrderIntent) -> SubmitResult:
        self._roll_day()
        intent.account = intent.account or self.account_id

        # 1 -- kill switches
        try:
            self.kill.check(strategy_id=self.ladder.strategy_id, account=intent.account)
        except KillSwitchTripped as exc:
            return self._record(SubmitResult(False, "kill_switch", message=str(exc), intent=intent))

        # 2 -- autonomy. Caps gate ENTRIES only: an order that reduces or closes
        # an existing position is never blocked by a trade count or a daily loss
        # limit. Blocking an exit would trap the client in the position the limit
        # was there to protect them from.
        is_paper = self.ladder.level <= Level.PAPER
        if not self._reduces_exposure(intent):
            try:
                self.ladder.check_daily(self.day.trades, self.day.pnl)
                self.ladder.authorize(intent, is_paper=is_paper)
            except AutonomyViolation as exc:
                if not is_paper:
                    return self._record(SubmitResult(False, "autonomy", message=str(exc), intent=intent))

        # 3 -- prop firm position limits
        if self.prop_firm:
            existing = self.positions.get(intent.account, intent.symbol)
            resulting = abs((existing.size if existing else 0.0) + intent.signed_size)
            ok, why = position_limit_ok(intent.symbol, resulting, self.prop_firm, self.prop_account)
            if not ok:
                return self._record(SubmitResult(False, "prop_limit", message=why, intent=intent))

        # 4 -- the hedge check. Nothing below this line runs if it refuses.
        check = self.positions.check(intent)
        if not check.allowed:
            self.sink(check.reason)
            return self._record(
                SubmitResult(False, "hedge", message=check.reason, intent=intent, reached_api=False)
            )

        # 5 -- paper/shadow or the real thing
        if is_paper:
            fill = self._paper_fill(intent)
            self.shadow_fills.append(fill)
            return self._record(
                SubmitResult(
                    True,
                    "paper",
                    ack=OrderAck("PAPER", True, "filled"),
                    message=f"paper fill {fill.side} {fill.size} {fill.symbol} @ {fill.price}",
                    intent=intent,
                )
            )

        ack = self.adapter.place(intent)
        if not ack.accepted:
            self.day.api_errors += 1
            self.kill.evaluate(
                strategy_id=self.ladder.strategy_id,
                account=intent.account,
                api_errors=self.day.api_errors,
            )
            return self._record(
                SubmitResult(False, "broker", ack=ack, message=ack.message, intent=intent, reached_api=True)
            )
        self.day.api_errors = 0
        self.day.trades += 1
        return self._record(
            SubmitResult(True, "broker", ack=ack, message=f"broker order {ack.broker_order_id}",
                         intent=intent, reached_api=True)
        )

    # --------------------------------------------------------------- fills
    def ingest_fills(self) -> list[BrokerFill]:
        fills = self.adapter.poll_fills()
        for fill in fills:
            self.positions.apply_fill(
                account=self.account_id,
                symbol=fill.symbol,
                signed_size=fill.signed_size,
                price=fill.price,
                strategy_id=self.ladder.strategy_id,
            )
        return fills

    def record_trade_result(self, pnl: float) -> None:
        self._roll_day()
        self.day.pnl += pnl
        self.day.consecutive_losses = self.day.consecutive_losses + 1 if pnl < 0 else 0
        tripped = self.kill.evaluate(
            strategy_id=self.ladder.strategy_id,
            account=self.account_id,
            daily_pnl=self.day.pnl,
            consecutive_losses=self.day.consecutive_losses,
        )
        for state in tripped:
            self.sink(f"[kill switch] {state.scope}:{state.name} tripped -- {state.reason}")
            self.flatten_all(reason="kill switch")

    def flatten_all(self, reason: str = "") -> None:
        for pos in self.positions.open_positions():
            if pos.account != self.account_id:
                continue
            self.adapter.flatten(pos.symbol)
            self.positions.flatten(pos.account, pos.symbol)
        if reason:
            self.sink(f"[flat] every position closed: {reason}")

    # -------------------------------------------------------------- helpers
    def _reduces_exposure(self, intent: OrderIntent) -> bool:
        existing = self.positions.get(intent.account, intent.symbol)
        if existing is None or existing.flat:
            return False
        return abs(existing.size + intent.signed_size) < abs(existing.size)

    def _paper_fill(self, intent: OrderIntent) -> BrokerFill:
        instrument = get_instrument(intent.symbol)
        price = intent.price or getattr(self.adapter, "last_price", {}).get(intent.symbol.upper(), 0.0)
        return BrokerFill(
            broker_order_id="PAPER",
            symbol=intent.symbol.upper(),
            side=intent.side,
            size=intent.size,
            price=instrument.round_to_tick(price) if price else price,
            fee=instrument.cost_per_side(price or 0.0, intent.size),
        )

    def _roll_day(self) -> None:
        today = self.clock()
        if self.day.day != today:
            self.day = DayState(day=today)

    def _record(self, result: SubmitResult) -> SubmitResult:
        self.submitted.append(result)
        return result

    def status(self) -> str:
        return "\n".join(
            [
                self.ladder.status(),
                self.positions.summary(),
                self.kill.summary(),
                f"[today] {self.day.trades} trade(s), P&L {self.day.pnl:,.2f}, "
                f"{self.day.consecutive_losses} consecutive loss(es)",
            ]
        )
