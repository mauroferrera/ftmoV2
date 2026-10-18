"""Test de integración para la ruta /api/market/institutional-volume."""

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


def test_get_institutional_volume_analysis(client, monkeypatch):
    from adapters.tradingview import tv_adapter

    # Mock de fetch_candles para asegurar ejecución offline determinista
    mock_candles = [
        {"time": "2026-10-31 10:00", "open": 1.115, "high": 1.120, "low": 1.114, "close": 1.118, "volume": 1200}
        for _ in range(15)
    ]
    monkeypatch.setattr(tv_adapter.TradingViewAdapter, "fetch_candles", lambda self, *args, **kwargs: mock_candles)

    resp = client.get("/api/market/institutional-volume?symbol=EURUSD&timeframe=M15")
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "EURUSD"
    assert data["cme_symbol"] == "CME:6E1!"
    assert "futures" in data
    assert data["futures"]["available"] is True
    assert data["futures"]["last_volume"] == 1200.0


def test_get_institutional_volume_confluence(client, monkeypatch):
    from adapters.tradingview import tv_adapter

    mock_candles = [
        {"time": "2026-10-31 10:00", "open": 1.115, "high": 1.120, "low": 1.114, "close": 1.118, "volume": 1000}
        for _ in range(10)
    ]
    # Vela ballena
    mock_candles.append({
        "time": "2026-10-31 10:15",
        "open": 1.116,
        "high": 1.120,
        "low": 1.110,
        "close": 1.119,
        "volume": 2500,
    })
    monkeypatch.setattr(tv_adapter.TradingViewAdapter, "fetch_candles", lambda self, *args, **kwargs: mock_candles)

    resp = client.get("/api/market/institutional-volume?symbol=EURUSD&direction=BUY&timeframe=M15")
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "EURUSD"
    assert data["whale_detected"] is True
    assert data["vol_ratio"] >= 2.0
    assert data["aligned"] is True
    assert "detail" in data

