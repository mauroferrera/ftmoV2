"""Pruebas para las rutas API del Plan de Trading 40 Minutos."""
from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from api.app import create_app
from api.runtime import Runtime


class FakeMarket:
    def estado(self):
        return {"estado": "ok", "conectado": True}

    def price(self, symbol: str):
        return {"bid": 1.1150, "ask": 1.1151}

    def pattern_data(self, symbol: str, timeframe: str = "M15", bars: int = 100):
        return {
            "candles": [{"open": 1.115, "high": 1.118, "low": 1.112, "close": 1.115}],
            "pdh": 1.1180,
            "pdl": 1.1120,
        }


def test_plan_40m_routes_get_and_trigger(tmp_path):
    p_file = str(tmp_path / "plan_route_test.json")
    rt = Runtime(market=FakeMarket())

    from api.services.plan_40m_service import Plan40mService
    rt.plan_service = Plan40mService(runtime=rt, state_file=p_file)
    rt.plan_service.fetch_all_macro_news = lambda now_utc=None: []

    app = create_app(runtime=rt)
    client = TestClient(app)

    # 1. Trigger manual
    resp_post = client.post("/api/analysis/eurusd-spot/plan-40m/trigger")
    assert resp_post.status_code == 200
    data_post = resp_post.json()
    assert data_post["status"] == "ok"
    assert data_post["plan_id"].startswith("PLAN-")

    # 2. Get Plan under /api/analysis/eurusd-spot/plan-40m
    resp_get1 = client.get("/api/analysis/eurusd-spot/plan-40m")
    assert resp_get1.status_code == 200
    data1 = resp_get1.json()
    assert "plan" in data1
    assert "vigilante" in data1
    assert "EURUSD" in data1["plan"]["pairs"]

    # 3. Get Plan under alias /api/analysis/plan-40m
    resp_get2 = client.get("/api/analysis/plan-40m")
    assert resp_get2.status_code == 200
    data2 = resp_get2.json()
    assert data2["plan"]["plan_id"] == data1["plan"]["plan_id"]
