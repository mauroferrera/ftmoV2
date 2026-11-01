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
def audit_client(tmp_store, monkeypatch):
    # `.env` deja `API_TOKEN` puesto al importar la app y ahora `POST
    # /api/audit/reconcile` pide token: sin aislarlo, estos tests comprobarían
    # el 401 en vez de la reconciliación.
    monkeypatch.delenv("API_TOKEN", raising=False)
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


def test_reconcile_pide_token_si_esta_configurado(audit_client, monkeypatch):
    """El POST escribe en base: con token configurado, sin él es 401 y con él 200."""
    monkeypatch.setenv("API_TOKEN", "secreto-de-prueba")

    sin_token = audit_client.post("/api/audit/reconcile?days=14")
    assert sin_token.status_code == 401

    con_token = audit_client.post(
        "/api/audit/reconcile?days=14",
        headers={"X-API-Token": "secreto-de-prueba"},
    )
    assert con_token.status_code == 200
    assert con_token.json()["status"] == "ok"


def test_performance_no_cuenta_las_marcas_de_posicion_abierta(audit_client, tmp_store):
    """Una salida PARCIAL no cierra el trade: no entra en el análisis cerrado."""
    tmp_store.save_trade_mark({
        "ticket": 9901,
        "symbol": "EURUSD",
        "direction": "BUY",
        "price_open": 1.0800,
        "price_close": 1.0840,
        "profit": 20.0,
        "r_multiple": 1.0,
        "exit_quality": "TP_PARTIAL",
        "outcome_json": {"is_open": True},
        "context_json": {},
    })
    audit_client.post("/api/audit/reconcile")

    resp = audit_client.get("/api/audit/performance")
    assert resp.status_code == 200
    data = resp.json()
    # La única marca CERRADA sigue siendo la del fixture; la parcial se declara.
    assert data["summary"]["closed_trades_total"] == 1
    assert data["summary"]["open_partial_trades"] == 1


