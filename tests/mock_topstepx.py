"""A mock TopstepX server.

The TopstepX adapter is written against the documented ProjectX Gateway API and
contract-tested against this, because live validation needs a funded account
(BLOCKERS.md). Every response shape here mirrors the documented one; when the
real API disagrees, this file is what gets corrected.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class MockResponse:
    status_code: int
    _payload: dict

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


@dataclass
class MockTopstepXServer:
    """Stands in for ``requests.Session``: the adapter's only transport seam."""

    token: str = "mock-jwt-token"
    balance: float = 50_000.0
    calls: list[tuple[str, str, dict]] = field(default_factory=list)
    orders: dict[str, dict] = field(default_factory=dict)
    positions_open: dict[str, dict] = field(default_factory=dict)
    fail_next: str | None = None

    def request(self, method: str, url: str, json: dict | None = None, headers: dict | None = None, timeout: float = 0):
        path = url.split("/api", 1)[-1]
        payload = json or {}
        self.calls.append((method, path, payload))

        if self.fail_next == path:
            self.fail_next = None
            return MockResponse(500, {"success": False, "errorMessage": "simulated server error"})

        if path == "/Auth/loginKey":
            if not payload.get("userName") or not payload.get("apiKey"):
                return MockResponse(401, {"success": False, "errorMessage": "bad credentials"})
            return MockResponse(200, {"success": True, "token": self.token, "errorCode": 0})

        if path == "/Account/search":
            return MockResponse(
                200,
                {
                    "success": True,
                    "accounts": [
                        {"id": 900001, "name": "50K Combine", "balance": self.balance,
                         "canTrade": True, "isVisible": True, "dayPnl": 0.0}
                    ],
                },
            )

        if path == "/Contract/search":
            text = str(payload.get("searchText", "")).upper()
            return MockResponse(
                200,
                {
                    "success": True,
                    "contracts": [
                        {"id": f"CON.F.US.{text}.Z25", "name": text, "description": f"{text} future",
                         "tickSize": 0.25, "tickValue": 0.5, "activeContract": True}
                    ],
                },
            )

        if path == "/Order/place":
            order_id = 5000 + len(self.orders) + 1
            self.orders[str(order_id)] = {**payload, "status": "filled"}
            contract = payload.get("contractId", "")
            side = int(payload.get("side", 0))
            size = float(payload.get("size", 0))
            existing = self.positions_open.get(contract, {"size": 0.0, "averagePrice": 20000.0})
            signed = size if side == 0 else -size
            new_size = existing["size"] + signed
            if abs(new_size) < 1e-9:
                self.positions_open.pop(contract, None)
            else:
                self.positions_open[contract] = {
                    "size": new_size, "averagePrice": existing["averagePrice"],
                }
            return MockResponse(200, {"success": True, "orderId": order_id, "errorCode": 0})

        if path == "/Order/cancel":
            order_id = str(payload.get("orderId"))
            if order_id in self.orders:
                self.orders[order_id]["status"] = "cancelled"
                return MockResponse(200, {"success": True})
            return MockResponse(200, {"success": False, "errorMessage": "unknown order"})

        if path == "/Trade/search":
            return MockResponse(
                200,
                {
                    "success": True,
                    "trades": [
                        {
                            "id": int(order_id),
                            "orderId": int(order_id),
                            "contractId": order["contractId"],
                            "side": order.get("side", 0),
                            "size": order.get("size", 1),
                            "price": 20000.0,
                            "fees": 0.37 * float(order.get("size", 1)),
                            "creationTimestamp": datetime.now(timezone.utc).isoformat(),
                        }
                        for order_id, order in self.orders.items()
                        if order.get("status") == "filled"
                    ],
                },
            )

        if path == "/Position/searchOpen":
            return MockResponse(
                200,
                {
                    "success": True,
                    "positions": [
                        {
                            "id": i + 1,
                            "contractId": contract,
                            "type": 1 if row["size"] > 0 else 2,
                            "size": abs(row["size"]),
                            "averagePrice": row["averagePrice"],
                        }
                        for i, (contract, row) in enumerate(self.positions_open.items())
                    ],
                },
            )

        if path == "/Position/closeContract":
            self.positions_open.pop(payload.get("contractId"), None)
            return MockResponse(200, {"success": True})

        return MockResponse(404, {"success": False, "errorMessage": f"no mock route for {path}"})
