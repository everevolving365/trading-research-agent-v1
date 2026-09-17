"""Broker adapters. One interface, every venue behind it (ability 53).

Adding a broker is implementing :class:`BrokerAdapter` and adding a config
entry. It is never a rewrite of the execution layer, and no adapter may contain
strategy logic, risk logic, or a hedge check -- those live above it, in the
router, so that no adapter can bypass them.

``docs/ADAPTER-INTERFACE.md`` is the contract the owner's existing Python robot
must satisfy to drop in as a first-class adapter.
"""
from __future__ import annotations

import json
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from ee_agent.execution.ledger import OrderIntent
from ee_agent.secrets.vault import get_secret


@dataclass
class OrderAck:
    broker_order_id: str
    accepted: bool
    status: str = "working"  # working | filled | rejected | cancelled
    message: str = ""
    submitted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BrokerFill:
    broker_order_id: str
    symbol: str
    side: str
    size: float
    price: float
    fee: float = 0.0
    filled_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    liquidity: str = "taker"

    @property
    def signed_size(self) -> float:
        return self.size if self.side.lower() in ("buy", "long") else -self.size

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AccountSnapshot:
    account_id: str
    balance: float
    equity: float
    day_pnl: float = 0.0
    open_positions: list[dict] = field(default_factory=list)
    buying_power: float = 0.0
    as_of: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


class BrokerAdapter(ABC):
    """The contract. Nine methods, no more.

    Implementations MUST NOT: decide position size, check risk, check for
    hedges, or modify the order. They translate and transmit, nothing else.
    """

    name: str = "abstract"
    supports_bracket: bool = False

    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def account(self) -> AccountSnapshot: ...

    @abstractmethod
    def place(self, intent: OrderIntent) -> OrderAck: ...

    @abstractmethod
    def cancel(self, broker_order_id: str) -> bool: ...

    @abstractmethod
    def poll_fills(self) -> list[BrokerFill]: ...

    @abstractmethod
    def positions(self) -> list[dict]: ...

    @abstractmethod
    def flatten(self, symbol: str | None = None) -> bool: ...

    def health(self) -> dict:
        return {"adapter": self.name, "connected": True, "latency_ms": 0.0}


# =================================================================== mock
class MockBroker(BrokerAdapter):
    """The reference adapter. Everything in Phase 8 completes against this.

    Deterministic when seeded, so contract tests are repeatable.
    """

    name = "mock"
    supports_bracket = True

    def __init__(
        self,
        account_id: str = "MOCK-1",
        balance: float = 50_000.0,
        latency_ms: float = 25.0,
        reject_rate: float = 0.0,
        slippage_ticks: float = 0.0,
        tick_size: float = 0.25,
        seed: int = 5,
    ):
        self.account_id = account_id
        self.balance = balance
        self.latency_ms = latency_ms
        self.reject_rate = reject_rate
        self.slippage_ticks = slippage_ticks
        self.tick_size = tick_size
        self.rng = random.Random(seed)
        self.connected = False
        self._orders: dict[str, dict] = {}
        self._pending_fills: list[BrokerFill] = []
        self._positions: dict[str, float] = {}
        self._avg_price: dict[str, float] = {}
        self.day_pnl = 0.0
        self.last_price: dict[str, float] = {}
        self.order_log: list[dict] = []

    # ---- market feed for the replay harness
    def set_price(self, symbol: str, price: float) -> None:
        self.last_price[symbol.upper()] = price

    def connect(self) -> bool:
        self.connected = True
        return True

    def disconnect(self) -> None:
        self.connected = False

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id=self.account_id,
            balance=self.balance,
            equity=self.balance + self.day_pnl,
            day_pnl=self.day_pnl,
            open_positions=self.positions(),
            buying_power=self.balance * 10,
        )

    def place(self, intent: OrderIntent) -> OrderAck:
        if not self.connected:
            return OrderAck("", False, "rejected", "not connected")
        order_id = f"MOCK-{len(self._orders) + 1:06d}"
        self.order_log.append({"ts": datetime.now(timezone.utc).isoformat(), **intent.to_dict()})
        if self.rng.random() < self.reject_rate:
            return OrderAck(order_id, False, "rejected", "simulated rejection", latency_ms=self.latency_ms)
        reference = intent.price or self.last_price.get(intent.symbol.upper(), 100.0)
        direction = 1.0 if intent.side.lower() in ("buy", "long") else -1.0
        fill_price = reference + direction * self.slippage_ticks * self.tick_size
        self._orders[order_id] = {"intent": intent.to_dict(), "status": "filled"}
        self._pending_fills.append(
            BrokerFill(
                broker_order_id=order_id,
                symbol=intent.symbol.upper(),
                side=intent.side,
                size=intent.size,
                price=fill_price,
                fee=0.37 * intent.size,
            )
        )
        symbol = intent.symbol.upper()
        signed = intent.signed_size
        prev = self._positions.get(symbol, 0.0)
        prev_avg = self._avg_price.get(symbol, fill_price)
        new = prev + signed
        if prev != 0 and (prev > 0) != (signed > 0):
            closed = min(abs(prev), abs(signed))
            self.day_pnl += (fill_price - prev_avg) * closed * (1 if prev > 0 else -1)
        if new == 0:
            self._positions.pop(symbol, None)
            self._avg_price.pop(symbol, None)
        else:
            if prev == 0 or (prev > 0) == (signed > 0):
                total = abs(prev) + abs(signed)
                self._avg_price[symbol] = (prev_avg * abs(prev) + fill_price * abs(signed)) / total
            else:
                self._avg_price[symbol] = fill_price
            self._positions[symbol] = new
        return OrderAck(order_id, True, "filled", latency_ms=self.latency_ms)

    def cancel(self, broker_order_id: str) -> bool:
        order = self._orders.get(broker_order_id)
        if order and order["status"] == "working":
            order["status"] = "cancelled"
            return True
        return False

    def poll_fills(self) -> list[BrokerFill]:
        out, self._pending_fills = self._pending_fills, []
        return out

    def positions(self) -> list[dict]:
        return [
            {"symbol": s, "size": q, "avg_price": self._avg_price.get(s, 0.0)}
            for s, q in self._positions.items()
            if q
        ]

    def flatten(self, symbol: str | None = None) -> bool:
        targets = [symbol.upper()] if symbol else list(self._positions)
        for sym in targets:
            size = self._positions.get(sym, 0.0)
            if size:
                self.place(
                    OrderIntent(
                        account=self.account_id,
                        symbol=sym,
                        side="sell" if size > 0 else "buy",
                        size=abs(size),
                        reason="flatten",
                    )
                )
        return True

    def health(self) -> dict:
        return {"adapter": self.name, "connected": self.connected, "latency_ms": self.latency_ms}


# =============================================================== TopstepX
class TopstepXAdapter(BrokerAdapter):
    """TopstepX, written against the documented ProjectX Gateway API.

    Built and contract-tested against a mock server (``tests/test_execution.py``)
    because live validation needs a funded account. Every call goes through
    :meth:`_request`, which is the single place the transport is mocked.
    """

    name = "topstepx"
    supports_bracket = True
    BASE_URL = "https://api.topstepx.com/api"

    def __init__(
        self,
        account_id: str | None = None,
        base_url: str | None = None,
        session: Any = None,
        timeout: float = 15.0,
    ):
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.account_id = account_id or get_secret("TOPSTEPX_ACCOUNT_ID") or ""
        self._session = session  # injectable for contract tests
        self._token: str | None = None
        self.timeout = timeout
        self.connected = False
        self._contract_ids: dict[str, str] = {}
        self._last_latency_ms = 0.0
        self._api_errors = 0

    # ---- transport -----------------------------------------------------
    def _client(self):
        if self._session is not None:
            return self._session
        import requests

        self._session = requests.Session()
        return self._session

    def _request(self, method: str, path: str, payload: dict | None = None, auth: bool = True) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json", "Accept": "text/plain"}
        if auth:
            if not self._token:
                raise RuntimeError("TopstepX: not authenticated; call connect() first")
            headers["Authorization"] = f"Bearer {self._token}"
        started = time.time()
        response = self._client().request(
            method, url, json=payload or {}, headers=headers, timeout=self.timeout
        )
        self._last_latency_ms = (time.time() - started) * 1000.0
        if getattr(response, "status_code", 200) >= 400:
            self._api_errors += 1
            raise RuntimeError(f"TopstepX {method} {path} -> {response.status_code}: {response.text[:200]}")
        self._api_errors = 0
        data = response.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"TopstepX {path} refused: {data.get('errorMessage') or data}")
        return data

    # ---- interface -----------------------------------------------------
    def connect(self) -> bool:
        username = get_secret("TOPSTEPX_USERNAME")
        api_key = get_secret("TOPSTEPX_API_KEY")
        if not username or not api_key:
            raise RuntimeError(
                "TopstepX needs TOPSTEPX_USERNAME and TOPSTEPX_API_KEY. "
                "Store them with `ee-agent secrets set TOPSTEPX_API_KEY` -- they go to the OS "
                "keychain, are never logged, and are never sent to any model."
            )
        data = self._request(
            "POST", "/Auth/loginKey", {"userName": username, "apiKey": api_key}, auth=False
        )
        self._token = data.get("token")
        self.connected = bool(self._token)
        if self.connected and not self.account_id:
            accounts = self.list_accounts()
            if accounts:
                self.account_id = str(accounts[0].get("id"))
        return self.connected

    def disconnect(self) -> None:
        self._token = None
        self.connected = False

    def list_accounts(self) -> list[dict]:
        data = self._request("POST", "/Account/search", {"onlyActiveAccounts": True})
        return data.get("accounts", [])

    def contract_id(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol not in self._contract_ids:
            data = self._request("POST", "/Contract/search", {"searchText": symbol, "live": False})
            contracts = data.get("contracts", [])
            if not contracts:
                raise RuntimeError(f"TopstepX has no contract matching {symbol!r}")
            self._contract_ids[symbol] = str(contracts[0]["id"])
        return self._contract_ids[symbol]

    def account(self) -> AccountSnapshot:
        accounts = self.list_accounts()
        row = next((a for a in accounts if str(a.get("id")) == str(self.account_id)), accounts[0] if accounts else {})
        balance = float(row.get("balance", 0.0))
        return AccountSnapshot(
            account_id=str(self.account_id),
            balance=balance,
            equity=balance,
            day_pnl=float(row.get("dayPnl", 0.0) or 0.0),
            open_positions=self.positions(),
            buying_power=float(row.get("balance", 0.0)),
        )

    def place(self, intent: OrderIntent) -> OrderAck:
        type_map = {"market": 2, "limit": 1, "stop": 4}
        payload = {
            "accountId": int(self.account_id),
            "contractId": self.contract_id(intent.symbol),
            "type": type_map.get(intent.order_type, 2),
            "side": 0 if intent.side.lower() in ("buy", "long") else 1,
            "size": int(intent.size),
            "customTag": (intent.strategy_id or "ee-agent")[:40],
        }
        if intent.order_type == "limit":
            payload["limitPrice"] = intent.price
        elif intent.order_type == "stop":
            payload["stopPrice"] = intent.price
        try:
            data = self._request("POST", "/Order/place", payload)
        except Exception as exc:
            return OrderAck("", False, "rejected", str(exc), latency_ms=self._last_latency_ms)
        return OrderAck(
            broker_order_id=str(data.get("orderId", "")),
            accepted=True,
            status="working",
            latency_ms=self._last_latency_ms,
        )

    def cancel(self, broker_order_id: str) -> bool:
        try:
            self._request(
                "POST", "/Order/cancel", {"accountId": int(self.account_id), "orderId": int(broker_order_id)}
            )
            return True
        except Exception:
            return False

    def poll_fills(self) -> list[BrokerFill]:
        data = self._request(
            "POST",
            "/Trade/search",
            {"accountId": int(self.account_id), "startTimestamp": _today_iso()},
        )
        out = []
        for row in data.get("trades", []):
            out.append(
                BrokerFill(
                    broker_order_id=str(row.get("orderId", "")),
                    symbol=str(row.get("contractId", "")),
                    side="buy" if int(row.get("side", 0)) == 0 else "sell",
                    size=float(row.get("size", 0)),
                    price=float(row.get("price", 0.0)),
                    fee=float(row.get("fees", 0.0) or 0.0),
                    filled_at=str(row.get("creationTimestamp", "")),
                )
            )
        return out

    def positions(self) -> list[dict]:
        data = self._request("POST", "/Position/searchOpen", {"accountId": int(self.account_id)})
        return [
            {
                "symbol": str(p.get("contractId", "")),
                "size": float(p.get("size", 0)) * (1 if int(p.get("type", 1)) == 1 else -1),
                "avg_price": float(p.get("averagePrice", 0.0)),
            }
            for p in data.get("positions", [])
        ]

    def flatten(self, symbol: str | None = None) -> bool:
        ok = True
        for pos in self.positions():
            if symbol and pos["symbol"].upper() != symbol.upper():
                continue
            try:
                self._request(
                    "POST",
                    "/Position/closeContract",
                    {"accountId": int(self.account_id), "contractId": pos["symbol"]},
                )
            except Exception:
                ok = False
        return ok

    def health(self) -> dict:
        return {
            "adapter": self.name,
            "connected": self.connected,
            "latency_ms": self._last_latency_ms,
            "api_errors": self._api_errors,
        }


class OwnerRobotAdapter(BrokerAdapter):
    """Placeholder for the owner's existing Python trading robot (Section 12).

    The robot is not in the repository yet. This adapter documents exactly what
    it must expose and fails with that message rather than pretending.
    See docs/ADAPTER-INTERFACE.md.
    """

    name = "owner_robot"

    def __init__(self, robot: Any = None):
        self.robot = robot

    def _require(self):
        if self.robot is None:
            raise NotImplementedError(
                "The owner's Python trading robot has not been supplied yet (BLOCKERS.md B-002). "
                "It must expose: connect(), account(), place(order), cancel(id), poll_fills(), "
                "positions(), flatten(symbol). See docs/ADAPTER-INTERFACE.md. Until then use the "
                "mock adapter, which the entire execution layer is tested against."
            )
        return self.robot

    def connect(self) -> bool:
        return bool(self._require().connect())

    def disconnect(self) -> None:
        self._require().disconnect()

    def account(self) -> AccountSnapshot:
        raw = self._require().account()
        return AccountSnapshot(**raw) if isinstance(raw, dict) else raw

    def place(self, intent: OrderIntent) -> OrderAck:
        raw = self._require().place(intent.to_dict())
        return OrderAck(**raw) if isinstance(raw, dict) else raw

    def cancel(self, broker_order_id: str) -> bool:
        return bool(self._require().cancel(broker_order_id))

    def poll_fills(self) -> list[BrokerFill]:
        return [BrokerFill(**f) if isinstance(f, dict) else f for f in self._require().poll_fills()]

    def positions(self) -> list[dict]:
        return list(self._require().positions())

    def flatten(self, symbol: str | None = None) -> bool:
        return bool(self._require().flatten(symbol))


ADAPTERS = {
    "mock": MockBroker,
    "topstepx": TopstepXAdapter,
    "owner_robot": OwnerRobotAdapter,
}


def get_adapter(name: str, **kwargs) -> BrokerAdapter:
    if name not in ADAPTERS:
        raise KeyError(f"Unknown broker '{name}'. Available: {', '.join(ADAPTERS)}")
    return ADAPTERS[name](**kwargs)


def _today_iso() -> str:
    from datetime import date

    return datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc).isoformat()
