"""Pruebas unitarias para el Servicio de Plan de Trading 40 Minutos y Comité Multi-Moneda."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from typing import Any, Dict, List
import pytest

from api.services.plan_40m_service import Plan40mService


class FakeMarketService:
    def price(self, symbol: str) -> Dict[str, Any]:
        p = 1.1150 if symbol == "EURUSD" else (1.3050 if symbol == "GBPUSD" else 1.3550)
        return {"bid": p, "ask": round(p + 0.0001, 5)}

    def pattern_data(self, symbol: str, timeframe: str = "M15", bars: int = 100) -> Dict[str, Any]:
        p = 1.1150 if symbol == "EURUSD" else 1.3050
        candles = []
        for i in range(50):
            candles.append({
                "open": p - 0.0010 + i * 0.00002,
                "high": p + 0.0015,
                "low": p - 0.0015,
                "close": p,
            })
        return {
            "candles": candles,
            "pdh": round(p + 0.0030, 5),
            "pdl": round(p - 0.0030, 5),
        }


def test_plan_40m_service_initialization(tmp_path):
    p_file = str(tmp_path / "trading_plan_test.json")
    svc = Plan40mService(runtime=None, state_file=p_file)
    assert svc.enabled is False
    assert svc.get_current_plan() is None


def test_plan_40m_window_and_regular_deliberation(tmp_path):
    p_file = str(tmp_path / "trading_plan_test.json")
    market = FakeMarketService()
    fake_rt = type("FakeRuntime", (), {"market": market, "autotrader": None, "execution": None})()

    svc = Plan40mService(runtime=fake_rt, state_file=p_file)

    # Forzamos una fecha fija sin noticias inminentes
    fixed_now = datetime(2026, 11, 9, 10, 0, 0, tzinfo=timezone.utc)

    # Mock de fetch_all_macro_news
    svc.fetch_all_macro_news = lambda now_utc=None: [
        {
            "time_utc": "15:30 UTC",
            "timestamp_utc": "2026-11-09T15:30:00Z",
            "currency": "USD",
            "impact": "High",
            "title": "US Core CPI",
            "minutes_to_event": 330,
        }
    ]

    plan = svc.deliberate_and_create_plan(force=True, reason="TEST_RUN", now_utc=fixed_now)

    assert plan["plan_id"].startswith("PLAN-20261109-1000")
    assert plan["duration_minutes"] == 40
    assert plan["valid_to_utc"] == "2026-11-09T10:40:00Z"
    assert "EURUSD" in plan["pairs"]
    assert "GBPUSD" in plan["pairs"]

    eur = plan["pairs"]["EURUSD"]
    assert eur["symbol"] == "EURUSD"
    assert eur["status"] == "ACTIVO"
    assert eur["mandate"] in ("COMPRA_EN_DISCOUNT", "VENTA_EN_PREMIUM", "ESPERAR_ZONA")
    assert os.path.exists(p_file)


def test_plan_40m_schedules_at_news_event(tmp_path):
    p_file = str(tmp_path / "trading_plan_test.json")
    market = FakeMarketService()
    fake_rt = type("FakeRuntime", (), {"market": market, "autotrader": None, "execution": None})()

    svc = Plan40mService(runtime=fake_rt, state_file=p_file)

    fixed_now = datetime(2026, 11, 9, 12, 10, 0, tzinfo=timezone.utc)

    # Noticia programada a las 12:30 UTC (a 20 min de now, antes de los 40 min)
    svc.fetch_all_macro_news = lambda now_utc=None: [
        {
            "time_utc": "12:30 UTC",
            "timestamp_utc": "2026-11-09T12:30:00Z",
            "currency": "USD",
            "impact": "High",
            "title": "Retail Sales",
            "minutes_to_event": 20,
        }
    ]

    plan = svc.deliberate_and_create_plan(force=True, reason="TEST_NEWS", now_utc=fixed_now)

    # La vigencia debe terminar a las 12:30:00Z en punto para re-deliberar con el dato
    assert plan["valid_to_utc"] == "2026-11-09T12:30:00Z"
    assert plan["duration_minutes"] == 20
    assert "Retail Sales" in plan["next_trigger_label"]
    assert plan["next_event_scheduled"]["title"] == "Retail Sales"


def test_plan_40m_symbol_pause_on_imminent_news(tmp_path):
    p_file = str(tmp_path / "trading_plan_test.json")
    market = FakeMarketService()
    fake_rt = type("FakeRuntime", (), {"market": market, "autotrader": None, "execution": None})()

    svc = Plan40mService(runtime=fake_rt, state_file=p_file)

    fixed_now = datetime(2026, 11, 9, 12, 25, 0, tzinfo=timezone.utc)

    # Noticia inminente en 5 minutos para USD
    svc.fetch_all_macro_news = lambda now_utc=None: [
        {
            "time_utc": "12:30 UTC",
            "timestamp_utc": "2026-11-09T12:30:00Z",
            "currency": "USD",
            "impact": "High",
            "title": "Fed Chair Speech",
            "minutes_to_event": 5,
        }
    ]

    plan = svc.deliberate_and_create_plan(force=True, reason="TEST_IMMINENT", now_utc=fixed_now)

    eur_plan = plan["pairs"]["EURUSD"]
    # Como USD tiene noticia High en 5 min, el mandato para EURUSD debe pausarse
    assert eur_plan["mandate"] == "PAUSA_POR_NOTICIAS"
    assert eur_plan["status"] == "PAUSA"
    assert "Fed Chair Speech" in eur_plan["rationale"]


def test_plan_with_vigilante_status(tmp_path):
    p_file = str(tmp_path / "trading_plan_test.json")
    market = FakeMarketService()

    class FakeExecution:
        def positions(self):
            return [
                {
                    "ticket": 123456,
                    "symbol": "EURUSD",
                    "type": 0,
                    "volume": 0.50,
                    "price_open": 1.1120,
                    "price_current": 1.1145,
                    "profit": 125.0,
                }
            ], None

        def management_view(self, ticket):
            return {"r_actual": 1.25, "be_applied": True}

    fake_rt = type("FakeRuntime", (), {
        "market": market,
        "execution": FakeExecution(),
        "autotrader": type("FakeAT", (), {"enabled": True, "_last_scan_at": "2026-11-09T10:00:00Z", "_last_rejection": None})(),
    })()

    svc = Plan40mService(runtime=fake_rt, state_file=p_file)
    svc.fetch_all_macro_news = lambda now_utc=None: []
    svc.deliberate_and_create_plan(force=True, reason="TEST_VIG")

    res = svc.get_plan_with_vigilante_status()
    assert "plan" in res
    assert "vigilante" in res
    vig = res["vigilante"]
    assert vig["autotrader_enabled"] is True
    assert vig["open_positions_count"] == 1
    assert vig["open_positions"][0]["ticket"] == 123456
    assert vig["open_positions"][0]["be_applied"] is True
