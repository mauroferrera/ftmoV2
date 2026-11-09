"""Pruebas para la ruta de cancelación de órdenes pendientes."""
from __future__ import annotations

import os
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import token_actual
from api.runtime import Runtime


class FakeExecutionAdapter:
    def __init__(self):
        self.cancelled_tickets = []

    def cancel_order(self, ticket: int):
        self.cancelled_tickets.append(ticket)
        return {"ok": True, "ticket": ticket, "action": "removed"}

    def orders(self, symbol=None):
        return [
            {
                "ticket": 987654,
                "symbol": "EURUSD",
                "type": "BUY_LIMIT",
                "volume": 0.1,
                "price_open": 1.1140,
                "sl": 1.1090,
                "tp": 1.1200,
            }
        ]


def test_cancel_order_endpoint():
    adapter = FakeExecutionAdapter()
    fake_rt = Runtime(execution=adapter)
    app = create_app(runtime=fake_rt)
    client = TestClient(app)

    token = token_actual()
    headers = {"X-API-Token": token} if token else {}

    # 1. Listar órdenes pendientes
    resp_get = client.get("/api/orders", headers=headers)
    assert resp_get.status_code == 200
    orders = resp_get.json().get("orders")
    assert len(orders) == 1
    assert orders[0]["ticket"] == 987654

    # 2. Cancelar la orden
    resp_cancel = client.post("/api/orders/987654/cancel", headers=headers)
    assert resp_cancel.status_code == 200
    res = resp_cancel.json()
    assert res["ok"] is True
    assert res["ticket"] == 987654
    assert 987654 in adapter.cancelled_tickets
