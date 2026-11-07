"""Tests unitarios para el módulo de Guía Operativa Diaria (core/daily_guide.py)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import analysis
from core.daily_guide import compute_daily_guide
from core.eurusd_spot_analysis_schema import validate_report


def test_daily_guide_ceiling_zone():
    # Precio cerca del PDH (1.1275)
    guide = compute_daily_guide(
        current_price=1.1272,
        pdh=1.1275,
        pdl=1.1205,
    )
    assert guide["symbol"] == "EURUSD"
    assert guide["diagnosis"]["zone"] == "TECHO_VENTA"
    assert guide["diagnosis"]["action_bias"] == "VENTA_FAVORECIDA"
    assert guide["levels"]["pdh_ceiling"] == 1.1275
    assert guide["levels"]["pdl_floor"] == 1.1205
    assert guide["levels"]["equilibrium_50"] == 1.1240
    assert "VENTA" in guide["diagnosis"]["action_plan"]


def test_daily_guide_floor_zone():
    # Precio cerca del PDL (1.1205)
    guide = compute_daily_guide(
        current_price=1.1208,
        pdh=1.1275,
        pdl=1.1205,
    )
    assert guide["diagnosis"]["zone"] == "PISO_COMPRA"
    assert guide["diagnosis"]["action_bias"] == "COMPRA_FAVORECIDA"
    assert "COMPRA" in guide["diagnosis"]["action_plan"]


def test_daily_guide_middle_dead_zone():
    # Precio en mitad de camino (1.1240)
    guide = compute_daily_guide(
        current_price=1.1240,
        pdh=1.1275,
        pdl=1.1205,
    )
    assert guide["diagnosis"]["zone"] == "CENTRO_NEUTRO"
    assert guide["diagnosis"]["action_bias"] == "ESPERAR_EXTREMOS"
    assert "No abras operaciones ahora" in guide["diagnosis"]["action_plan"]


def test_daily_guide_traffic_light():
    # Sin noticias de alto impacto -> Luz verde
    g_green = compute_daily_guide(
        current_price=1.1240,
        pdh=1.1275,
        pdl=1.1205,
        calendar_events=[{"title": "German Trade", "impact": "Low"}],
    )
    assert g_green["traffic_light"] == "LUZ_VERDE"

    # Con noticia de alto impacto -> Precaución
    g_warn = compute_daily_guide(
        current_price=1.1240,
        pdh=1.1275,
        pdl=1.1205,
        calendar_events=[{"title": "Non-Farm Payrolls", "impact": "High", "time_utc": "13:30", "currency": "USD"}],
    )
    assert g_warn["traffic_light"] == "PRECAUCION"
    assert len(g_warn["danger_hours"]) == 1
    assert "13:30 UTC" in g_warn["danger_hours"][0]


def test_daily_guide_risk_calculator():
    guide = compute_daily_guide(
        current_price=1.1240,
        pdh=1.1280,
        pdl=1.1200,
        account_balance=2000.0,
        daily_loss_budget=100.0,
    )
    rm = guide["risk_management"]
    assert rm["account_balance"] == 2000.0
    assert rm["daily_loss_budget"] == 100.0
    assert rm["risk_per_trade_usd"] == 45.0
    assert rm["suggested_lot"] > 0
    assert rm["expected_win_usd"] > 0


def test_daily_guide_schema_and_route():
    app = FastAPI()
    app.include_router(analysis.router)
    client = TestClient(app)

    res = client.get("/api/analysis/eurusd-spot/daily-guide")
    assert res.status_code == 200
    data = res.json()
    assert "levels" in data
    assert "diagnosis" in data
    assert "risk_management" in data

    # Schema validation
    payload = {
        "meta": {"pair": "EURUSD", "generated_at_utc": "2026-11-04T12:00:00Z", "run_key": "2026-11-04_14", "run_time": "14"},
        "bias": "neutral",
        "confidence": 60,
        "sources": [{"url": "https://example.com", "sentiment": "neutral"}],
        "daily_guide": data,
    }
    validated = validate_report(payload)
    assert "daily_guide" in validated
    assert validated["daily_guide"]["symbol"] == "EURUSD"

