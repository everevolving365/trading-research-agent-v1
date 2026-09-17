# Broker adapter interface

What the owner's existing Python trading robot must expose to drop in as a
first-class adapter (Section 2.2, Section 12).

The whole execution layer is already built and tested against `MockBroker`,
which implements exactly this interface. Nothing below is aspirational: the
router, the position ledger, the autonomy ladder and the kill switches all talk
to this and nothing else.

## The contract

Implement `ee_agent.execution.adapters.BrokerAdapter`. Seven required methods,
plus `health()`:

```python
class BrokerAdapter(ABC):
    name: str                      # "owner_robot"
    supports_bracket: bool         # True if the venue accepts stop+target with the entry

    def connect(self) -> bool: ...
    def disconnect(self) -> None: ...
    def account(self) -> AccountSnapshot: ...
    def place(self, intent: OrderIntent) -> OrderAck: ...
    def cancel(self, broker_order_id: str) -> bool: ...
    def poll_fills(self) -> list[BrokerFill]: ...
    def positions(self) -> list[dict]: ...
    def flatten(self, symbol: str | None = None) -> bool: ...
    def health(self) -> dict: ...
```

### What an adapter must NOT do

This matters more than what it must do.

- **No sizing.** The spec's `risk.size` decides size. An adapter that resizes an
  order is trading a different strategy than the one that was proven.
- **No risk checks.** They happen above, in the router, where they are logged.
- **No hedge checks.** The global position ledger owns that (hard rule 3). An
  adapter that also checks creates two sources of truth, and the one that is
  wrong will be the one nobody is looking at.
- **No retries that change the order.** Retry the same order or fail loudly.
  Silently converting a limit to a market fill is how a backtest stops matching
  reality.
- **No strategy logic of any kind.**

An adapter translates and transmits. That is the whole job.

## The types

```python
@dataclass
class OrderIntent:            # what the router hands you
    account: str
    symbol: str               # registry symbol, e.g. "MNQ" -- map it to your venue's id
    side: str                 # "buy" | "sell"
    size: float
    order_type: str = "market"   # "market" | "limit" | "stop"
    price: float | None = None   # limit or stop price
    strategy_id: str = ""        # goes in the broker's order tag where possible
    reason: str = ""             # "entry" | "stop" | "target" | "session_flatten" | ...

@dataclass
class OrderAck:               # what you return immediately
    broker_order_id: str
    accepted: bool
    status: str = "working"   # "working" | "filled" | "rejected" | "cancelled"
    message: str = ""
    submitted_at: str = <iso8601 UTC>
    latency_ms: float = 0.0   # feeds the latency kill switch -- measure it honestly

@dataclass
class BrokerFill:             # what poll_fills() returns
    broker_order_id: str
    symbol: str
    side: str                 # "buy" | "sell"
    size: float
    price: float
    fee: float = 0.0
    filled_at: str = <iso8601 UTC>
    liquidity: str = "taker"  # "maker" | "taker", where the venue tells you

@dataclass
class AccountSnapshot:
    account_id: str
    balance: float
    equity: float
    day_pnl: float = 0.0
    open_positions: list[dict] = []   # [{"symbol", "size" (signed), "avg_price"}]
    buying_power: float = 0.0
    as_of: str = <iso8601 UTC>
```

`positions()` returns `[{"symbol": str, "size": float, "avg_price": float}]`
with **signed** size: positive long, negative short. Getting the sign wrong is
the single most dangerous bug possible here, because the hedge check reads it.

## Dropping the owner's robot in

`OwnerRobotAdapter` is already wired into the adapter table under the name
`owner_robot`. It takes the robot object and forwards to it:

```python
from ee_agent.execution.adapters import OwnerRobotAdapter
from my_robot import Robot

adapter = OwnerRobotAdapter(robot=Robot(...))
```

The robot needs the same seven methods (dicts are accepted where dataclasses are
expected; the adapter converts). Until it is supplied, `OwnerRobotAdapter`
raises with this document's path in the message rather than pretending to work.

## Proving an adapter works

Run the contract tests against it:

```bash
python -m pytest tests/test_execution.py -k contract -q
```

They check, against your adapter:

1. `connect()` succeeds and `account()` returns a positive balance.
2. `place()` returns an ack with a non-empty `broker_order_id`.
3. The fill that comes back from `poll_fills()` has the **same sign** as the
   order that produced it.
4. `positions()` reflects the fill, signed correctly.
5. `flatten()` leaves `positions()` empty.
6. A rejected order returns `accepted=False` and does not raise.
7. `health()` reports latency.

Then run it through the replay harness, which puts it under the real live code
path with recorded history:

```bash
ee-agent replay library/sweep-return-v1/spec.yaml --fixtures --sessions 5
```

## Order tagging

Where the venue allows a client tag, put `intent.strategy_id` in it. It is what
makes an external fill traceable back to the spec hash that produced the signal,
and therefore what makes the verified signal ledger meaningful for live trades.
