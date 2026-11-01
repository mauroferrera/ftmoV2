"""Tests de integración para las rutas de auditoría y reconciliación (M20)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.runtime import Runtime
from api.services.reconciliation import ReconciliationService


class FakeMarketWithDeals:
    def __init__(self, deals: list[dict] | None = None) -> None:
        self.deals = deals or []

    def history_deals(self, days: int = 30, position_id: int | None = None, ticket: int | None = None):
        return list(self.deals)

    def candles(self, symbol: str, timeframe: str):
        return {"candles": []}


@pytest.fixture
def audit_client(tmp_store):
    market = FakeMarketWithDeals(deals=[
        {
            "ticket": 7001,
            "order": 8801,
            "time": 1700000000,
            "type": 0,
            "entry": 0,
            "position_id": 8801,
            "volume": 0.1,
            "price": 1.0800,
            "symbol": "EURUSD",
        },
        {
            "ticket": 7002,
            "order": 8802,
            "time": 1700003600,
            "type": 1,
            "entry": 1,
            "position_id": 8801,
            "volume": 0.1,
            "price": 1.0840,
            "profit": 40.0,
            "symbol": "EURUSD",
            "reason": 5,  # TP
        },
    ])
    rt = Runtime(store=tmp_store, market=market)
    rt.reconciliation = ReconciliationService(market=market, store=tmp_store)

    app = create_app(runtime=rt)
    return TestClient(app)


def test_post_audit_reconcile_endpoint(audit_client):
    resp = audit_client.post("/api/audit/reconcile?days=14")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["marks_saved"] == 1
    assert data["reconciled_positions"] == 1


def test_get_audit_performance_endpoint(audit_client):
    # Primero reconciliar
    audit_client.post("/api/audit/reconcile")

    # Luego consultar performance
    resp = audit_client.get("/api/audit/performance")
    assert resp.status_code == 200
    data = resp.json()

    assert "summary" in data
    assert "score_buckets" in data
    assert "by_symbol" in data
    assert "by_exit_reason" in data

    summary = data["summary"]
    assert summary["closed_trades_total"] == 1
    assert summary["wins"] == 1
    assert summary["winrate_pct"] == 100.0


def test_get_trade_marks_endpoint(audit_client):
    audit_client.post("/api/audit/reconcile")

    resp = audit_client.get("/api/audit/marks?limit=10")
    assert resp.status_code == 200
    marks = resp.json()
    assert isinstance(marks, list)
    assert len(marks) == 1
    assert marks[0]["ticket"] == 8801
    assert marks[0]["symbol"] == "EURUSD"
