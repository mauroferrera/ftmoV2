"""Tests de AutoTrader y rutas de Auditoría."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.runtime import Runtime


class FakeStore:
    def __init__(self):
        self.watcher_cfg = {"enabled": True, "scan_interval_sec": 30}
        self.trading_cfg = {"symbols": ["EURUSD"], "min_score": 50.0}
        self.setup_log = []

    def get_watcher_config(self):
        return self.watcher_cfg

    def get_trading_config(self):
        return self.trading_cfg

    def get_setup_state(self, sym, tf):
        return None

    def log_setup(self, row):
        self.setup_log.append(row)
        return len(self.setup_log)

    def new_setup_state(self, sym, tf, row):
        pass

    def connect(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE setup_log (
                id INTEGER PRIMARY KEY, symbol TEXT, timeframe TEXT, direction TEXT,
                verdict TEXT, score REAL, breakdown_json TEXT, entry REAL, sl REAL,
                target REAL, invalidate_level REAL, validated INTEGER, reject_reasons TEXT,
                trade_result_json TEXT, source TEXT, created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE trade_marks (
                id INTEGER PRIMARY KEY, ticket INTEGER, symbol TEXT, direction TEXT,
                setup_id INTEGER, volume REAL, price_open REAL, price_close REAL,
                sl REAL, tp REAL, profit REAL, r_multiple REAL, exit_reason INTEGER,
                exit_quality TEXT, duration_s REAL, capture_path TEXT, outcome_json TEXT,
                context_json TEXT, source TEXT, created_at TEXT
            )
        """)
        return conn


class FakeMarket:
    def snapshot(self, sym, tf, **kwargs):
        return None

    def spec(self, sym):
        return None

    def price(self, sym):
        return {"bid": 1.1000, "ask": 1.1002}


class FakeExecution:
    def positions(self):
        return [], None

    def management_view(self, ticket):
        return None

    def apply_management(self, ticket, force=False):
        return {}, 200

    def execute_market_trade(self, **kwargs):
        return {"ticket": 9999, "price": 1.1000, "volume": 0.5}, 200


def test_autotrader_status_and_toggle(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    store = FakeStore()
    market = FakeMarket()
    rt = Runtime(store=store, market=market, execution=FakeExecution())
    app = create_app(runtime=rt)
    client = TestClient(app)

    # 1. Status inicial
    res = client.get("/api/autotrader/status")
    assert res.status_code == 200
    data = res.json()
    assert data["enabled"] is False

    # 2. Toggle On
    res_on = client.post("/api/autotrader/toggle", json={"enabled": True, "interval_sec": 15})
    assert res_on.status_code == 200
    assert res_on.json()["enabled"] is True

    # 3. Status On
    res_st = client.get("/api/autotrader/status")
    assert res_st.json()["enabled"] is True
    assert res_st.json()["scan_interval_sec"] == 15

    # 4. Toggle Off
    res_off = client.post("/api/autotrader/toggle", json={"enabled": False})
    assert res_off.status_code == 200
    assert res_off.json()["enabled"] is False


def test_audit_performance_route():
    store = FakeStore()
    rt = Runtime(store=store, market=FakeMarket(), execution=FakeExecution())
    app = create_app(runtime=rt)
    client = TestClient(app)

    res = client.get("/api/audit/performance")
    assert res.status_code == 200
    data = res.json()
    assert "summary" in data
    assert data["summary"]["total_setups_evaluated"] == 0
    assert data["summary"]["winrate_pct"] == 0.0
