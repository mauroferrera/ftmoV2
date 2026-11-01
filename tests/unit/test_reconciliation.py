"""Pruebas unitarias del servicio de reconciliación de resultados (M20)."""

from __future__ import annotations

import pytest

from api.services.reconciliation import (
    DEAL_ENTRY_IN,
    DEAL_ENTRY_OUT,
    DEAL_REASON_CLIENT,
    DEAL_REASON_EXPERT,
    DEAL_REASON_SL,
    DEAL_REASON_TP,
    ReconciliationService,
)
from database import store as test_store


class FakeMarketWithDeals:
    """Market falso con historial de deals inyectable."""

    def __init__(self, deals: list[dict] | None = None) -> None:
        self.deals = deals or []
        self.candles_data = {}

    def history_deals(self, days: int = 30, position_id: int | None = None, ticket: int | None = None):
        if position_id is not None:
            return [d for d in self.deals if d.get("position_id") == position_id]
        if ticket is not None:
            return [d for d in self.deals if d.get("ticket") == ticket]
        return list(self.deals)

    def candles(self, symbol: str, timeframe: str):
        return {"candles": self.candles_data.get((symbol, timeframe), [])}


@pytest.fixture
def clean_db(tmp_store):
    """Base de datos limpia usando el seam tmp_store."""
    return tmp_store


def test_reconcile_sin_deals_devuelve_cero(clean_db):
    market = FakeMarketWithDeals(deals=[])
    service = ReconciliationService(market=market, store=clean_db)
    res = service.reconcile_closed_trades()
    assert res["status"] == "ok"
    assert res["reconciled_positions"] == 0
    assert res["marks_saved"] == 0


def test_reconcile_trade_ganador_con_tp_y_setup(clean_db):
    # 1. Crear setup_log previo
    setup_id = clean_db.log_setup({
        "symbol": "EURUSD",
        "direction": "BUY",
        "verdict": "APPROVED",
        "score": 75.0,
        "entry": 1.0850,
        "sl": 1.0830,  # 20 pips de riesgo
        "target": 1.0890,  # 40 pips = 2R
        "validated": 1,
        "trade_result_json": {"order": 9991},
    })

    # 2. Inyectar deals en market para position_id 9991
    deals = [
        {
            "ticket": 1001,
            "order": 9991,
            "time": 1700000000,
            "type": 0,  # BUY
            "entry": DEAL_ENTRY_IN,
            "position_id": 9991,
            "volume": 0.5,
            "price": 1.0850,
            "commission": -3.5,
            "swap": 0.0,
            "profit": 0.0,
            "fee": 0.0,
            "symbol": "EURUSD",
        },
        {
            "ticket": 1002,
            "order": 9992,
            "time": 1700003600,
            "type": 1,  # SELL (cierre)
            "entry": DEAL_ENTRY_OUT,
            "position_id": 9991,
            "volume": 0.5,
            "price": 1.0890,  # +40 pips
            "commission": -3.5,
            "swap": 0.0,
            "profit": 200.0,
            "fee": 0.0,
            "symbol": "EURUSD",
            "reason": DEAL_REASON_TP,
        },
    ]

    market = FakeMarketWithDeals(deals=deals)
    service = ReconciliationService(market=market, store=clean_db)
    res = service.reconcile_closed_trades()

    assert res["status"] == "ok"
    assert res["reconciled_positions"] == 1
    assert res["marks_saved"] == 1

    # Verificar trade mark guardado
    mark = clean_db.get_trade_mark(9991)
    assert mark is not None
    assert mark["symbol"] == "EURUSD"
    assert mark["direction"] == "BUY"
    assert mark["setup_id"] == setup_id
    assert mark["price_open"] == pytest.approx(1.0850)
    assert mark["price_close"] == pytest.approx(1.0890)
    assert mark["r_multiple"] == pytest.approx(2.0)
    assert mark["exit_quality"] == "TP"
    assert mark["profit"] == pytest.approx(193.0)  # 200 - 3.5 - 3.5 = 193.0
    assert mark["duration_s"] == 3600.0

    # Verificar que el setup_log se actualizó con status CLOSED
    setup_row = clean_db.get_setup_row(setup_id)
    assert setup_row["trade_result_json"]["status"] == "CLOSED"
    assert setup_row["trade_result_json"]["r_multiple"] == pytest.approx(2.0)


def test_reconcile_trade_perdedor_sl(clean_db):
    setup_id = clean_db.log_setup({
        "symbol": "GBPUSD",
        "direction": "BUY",
        "verdict": "APPROVED",
        "score": 68.0,
        "entry": 1.2500,
        "sl": 1.2480,  # 20 pips riesgo
        "target": 1.2540,
        "validated": 1,
        "trade_result_json": {"order": 9992},
    })

    deals = [
        {
            "ticket": 2001,
            "order": 9992,
            "time": 1700010000,
            "type": 0,
            "entry": DEAL_ENTRY_IN,
            "position_id": 9992,
            "volume": 0.2,
            "price": 1.2500,
            "commission": -1.5,
            "swap": 0.0,
            "profit": 0.0,
            "symbol": "GBPUSD",
        },
        {
            "ticket": 2002,
            "order": 9993,
            "time": 1700011800,
            "type": 1,
            "entry": DEAL_ENTRY_OUT,
            "position_id": 9992,
            "volume": 0.2,
            "price": 1.2480,  # -20 pips
            "commission": -1.5,
            "swap": 0.0,
            "profit": -40.0,
            "symbol": "GBPUSD",
            "reason": DEAL_REASON_SL,
        },
    ]

    market = FakeMarketWithDeals(deals=deals)
    service = ReconciliationService(market=market, store=clean_db)
    res = service.reconcile_closed_trades()

    assert res["marks_saved"] == 1
    mark = clean_db.get_trade_mark(9992)
    assert mark is not None
    assert mark["r_multiple"] == pytest.approx(-1.0)
    assert mark["exit_quality"] == "SL"
    assert mark["profit"] == pytest.approx(-43.0)


def test_reconcile_break_even(clean_db):
    setup_id = clean_db.log_setup({
        "symbol": "USDJPY",
        "direction": "SELL",
        "verdict": "APPROVED",
        "score": 82.0,
        "entry": 150.00,
        "sl": 150.40,
        "target": 149.20,
        "validated": 1,
        "trade_result_json": {"order": 9993},
    })

    deals = [
        {
            "ticket": 3001,
            "order": 9993,
            "time": 1700020000,
            "type": 1,  # SELL
            "entry": DEAL_ENTRY_IN,
            "position_id": 9993,
            "volume": 0.3,
            "price": 150.00,
            "symbol": "USDJPY",
        },
        {
            "ticket": 3002,
            "order": 9994,
            "time": 1700025000,
            "type": 0,
            "entry": DEAL_ENTRY_OUT,
            "position_id": 9993,
            "volume": 0.3,
            "price": 150.01,  # Cierre en BE (aprox 0 pips)
            "profit": -2.0,
            "symbol": "USDJPY",
            "reason": DEAL_REASON_SL,  # Fue cerrado por el SL que estaba movido a BE
        },
    ]

    market = FakeMarketWithDeals(deals=deals)
    service = ReconciliationService(market=market, store=clean_db)
    service.reconcile_closed_trades()

    mark = clean_db.get_trade_mark(9993)
    assert mark is not None
    assert mark["exit_quality"] == "BE"
    assert abs(mark["r_multiple"]) <= 0.15


def test_reconcile_idempotencia(clean_db):
    deals = [
        {"ticket": 4001, "order": 9994, "time": 1700030000, "type": 0, "entry": DEAL_ENTRY_IN, "position_id": 9994, "volume": 0.1, "price": 1.1000, "symbol": "EURUSD"},
        {"ticket": 4002, "order": 9995, "time": 1700031000, "type": 1, "entry": DEAL_ENTRY_OUT, "position_id": 9994, "volume": 0.1, "price": 1.1020, "profit": 20.0, "symbol": "EURUSD", "reason": DEAL_REASON_TP},
    ]
    market = FakeMarketWithDeals(deals=deals)
    service = ReconciliationService(market=market, store=clean_db)

    # Correr dos veces seguidas
    r1 = service.reconcile_closed_trades()
    r2 = service.reconcile_closed_trades()

    assert r1["marks_saved"] == 1
    assert r2["marks_saved"] == 1

    marks = clean_db.list_trade_marks()
    # Solo debe existir UNA fila para position 9994
    matching = [m for m in marks if m["ticket"] == 9994]
    assert len(matching) == 1


def test_performance_analysis_metricas(clean_db):
    # Inyectar marcas con diferentes scores y resultados
    clean_db.save_trade_mark({
        "ticket": 5001,
        "symbol": "EURUSD",
        "direction": "BUY",
        "profit": 150.0,
        "r_multiple": 1.5,
        "exit_quality": "TP",
        "context_json": {"score": 75.0},
    })
    clean_db.save_trade_mark({
        "ticket": 5002,
        "symbol": "EURUSD",
        "direction": "BUY",
        "profit": -100.0,
        "r_multiple": -1.0,
        "exit_quality": "SL",
        "context_json": {"score": 62.0},
    })
    clean_db.save_trade_mark({
        "ticket": 5003,
        "symbol": "XAUUSD",
        "direction": "SELL",
        "profit": 300.0,
        "r_multiple": 2.0,
        "exit_quality": "TP",
        "context_json": {"score": 85.0},
    })

    service = ReconciliationService(market=FakeMarketWithDeals(), store=clean_db)
    analysis = service.get_performance_analysis()

    summary = analysis["summary"]
    assert summary["closed_trades_total"] == 3
    assert summary["wins"] == 2
    assert summary["losses"] == 1
    assert summary["winrate_pct"] == pytest.approx(66.7, abs=0.1)
    assert summary["total_profit_usd"] == pytest.approx(350.0)

    # Expectancy: (0.667 * 1.75) - (0.333 * 1.0) = ~0.83R
    assert summary["expectancy_r"] > 0

    # Buckets
    buckets = analysis["score_buckets"]
    assert buckets["70-80"]["trades"] == 1
    assert buckets["70-80"]["wins"] == 1
    assert buckets["60-70"]["losses"] == 1
    assert buckets["80+"]["wins"] == 1

    # Por símbolo
    syms = analysis["by_symbol"]
    assert syms["EURUSD"]["trades"] == 2
    assert syms["XAUUSD"]["trades"] == 1
