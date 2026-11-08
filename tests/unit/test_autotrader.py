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


def test_autotrader_preflight_blocks_when_terminal_algo_trading_off(monkeypatch, tmp_path):
    import asyncio
    from api.services.autotrader import AutoTraderService

    class MockMarketWithAlgoOff:
        def terminal_info(self):
            return {"connected": True, "trade_allowed": False}

    async def _test():
        rt = Runtime(market=MockMarketWithAlgoOff(), execution=FakeExecution(), store=FakeStore())
        svc = AutoTraderService(runtime=rt, scan_interval_sec=10)
        monkeypatch.setattr(svc, "_state_file_path", lambda: str(tmp_path / "autotrader_state.json"))

        res = await svc.run_cycle()
        assert "MT5 Algo Trading OFF" in (svc.status()["last_error"] or "")
        assert res["trades_executed"] == []
        assert any("MT5 Algo Trading OFF" in r for r in res["skipped_reasons"])

    asyncio.run(_test())


def test_autotrader_rejection_backoff_and_persistence(monkeypatch, tmp_path):
    import asyncio
    from api.services.autotrader import AutoTraderService

    class MockExecutionReject:
        def execute_market_trade(self, *args, **kwargs):
            return {"error": "Trading algorítmico deshabilitado en el terminal MT5.", "retcode": 10027}, 502

        def positions(self, *args, **kwargs):
            return [], None

        def management_view(self, *args, **kwargs):
            return None

    class MockWatcher:
        def escanear(self, *args, **kwargs):
            return {"events": [{"event": "new", "symbol": "EURUSD", "direction": "BUY", "score": 80.0, "entry": 1.12, "sl": 1.118, "tp": 1.126}]}

    async def _test():
        rt = Runtime(market=FakeMarket(), execution=MockExecutionReject(), store=FakeStore())
        rt.watcher = MockWatcher()
        svc = AutoTraderService(runtime=rt, scan_interval_sec=10)
        monkeypatch.setattr(svc, "_state_file_path", lambda: str(tmp_path / "autotrader_state.json"))

        # Test persistencia
        assert svc.should_autostart() is False
        await svc.start()
        assert svc.should_autostart() is True

        # Test ejecución con rechazo y backoff
        res = await svc.run_cycle()
        assert res["trades_executed"] == []
        status = svc.status()
        assert "Trading algorítmico deshabilitado" in (status["last_rejection"] or "")
        assert "EURUSD" in svc._symbol_cooldowns
        assert len(svc._symbol_cooldowns) >= 1

        await svc.stop()
        assert svc.should_autostart() is False

    asyncio.run(_test())


def test_autotrader_triple_confluence_filter():
    from datetime import datetime, timezone
    from api.services.autotrader import AutoTraderService

    svc = AutoTraderService()

    # 1. Fuera de Killzone (ej. 03:00 UTC) -> Rechazado
    fuera_kz = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    ok, reason = svc._check_triple_confluence(
        symbol="EURUSD",
        direction="BUY",
        planned_entry=1.1200,
        current_price=1.1200,
        momento=fuera_kz,
        killzone_info=None,
    )
    assert ok is False
    assert "Killzone" in reason

    # 2. Dentro de Killzone (08:00 UTC) pero precio alejado de la zona (+10 pips) -> Rechazado
    dentro_kz = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    ok_drift, reason_drift = svc._check_triple_confluence(
        symbol="GBPUSD",
        direction="BUY",
        planned_entry=1.2800,
        current_price=1.2810,  # 10 pips arriba
        momento=dentro_kz,
        killzone_info={"in_killzone": True},
    )
    assert ok_drift is False
    assert "alejado" in reason_drift

    # 3. Dentro de Killzone y precio en la zona (1.1201 vs 1.1200 = 1 pip) -> Aprobado
    ok_pass, reason_pass = svc._check_triple_confluence(
        symbol="USDJPY",
        direction="BUY",
        planned_entry=155.00,
        current_price=155.01,
        momento=dentro_kz,
        killzone_info={"in_killzone": True},
    )
    assert ok_pass is True
    assert reason_pass is None


