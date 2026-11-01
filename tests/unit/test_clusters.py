"""Pruebas unitarias para core/clusters.py y dynamic_risk_pct (P1 - Audit #8, #9)."""

import pytest

from core import clusters, prop_rules


def test_get_symbol_clusters_known_symbols():
    # AUDUSD pertenece a AUD y USD
    audusd = clusters.get_symbol_clusters("AUDUSD")
    assert "AUD" in audusd
    assert "USD" in audusd

    # XAUUSD pertenece a XAU y USD
    xauusd = clusters.get_symbol_clusters("XAUUSD")
    assert "XAU" in xauusd
    assert "USD" in xauusd

    # EURJPY pertenece a EUR y JPY
    eurjpy = clusters.get_symbol_clusters("EURJPY")
    assert "EUR" in eurjpy
    assert "JPY" in eurjpy

    # AUDCAD pertenece a AUD y CAD
    audcad = clusters.get_symbol_clusters("AUDCAD")
    assert "AUD" in audcad
    assert "CAD" in audcad


def test_get_symbol_clusters_fallback():
    # Par desconocido de 6 caracteres (ej. NZDCHF)
    res = clusters.get_symbol_clusters("NZDCHF")
    assert res == ["CHF", "NZD"]


def test_calculate_open_risk_r_break_even():
    # BUY con SL en o por encima del precio de entrada -> 0.0R
    pos_buy_be = {"type": "BUY", "price_open": 1.1000, "sl": 1.1005}
    assert clusters.calculate_open_risk_r(pos_buy_be) == 0.0

    # SELL con SL en o por debajo del precio de entrada -> 0.0R
    pos_sell_be = {"type": "SELL", "price_open": 1.1000, "sl": 1.0995}
    assert clusters.calculate_open_risk_r(pos_sell_be) == 0.0


def test_calculate_open_risk_r_not_at_be():
    # BUY no ha llegado a BE
    pos_buy = {"type": "BUY", "price_open": 1.1000, "sl": 1.0980}
    assert clusters.calculate_open_risk_r(pos_buy) == 1.0

    # Con risk_r explícito
    pos_media = {"type": "BUY", "price_open": 1.1000, "sl": 1.0980, "risk_r": 0.5}
    assert clusters.calculate_open_risk_r(pos_media) == 0.5


def test_check_exposure_limits_max_positions_per_symbol():
    open_positions = [{"symbol": "AUDUSD", "type": "BUY", "price_open": 0.6500, "sl": 0.6480}]

    ok, reason, code = clusters.check_exposure_limits(
        open_positions=open_positions,
        new_symbol="AUDUSD",
        new_risk_r=1.0,
        max_positions_per_symbol=1,
    )
    assert not ok
    assert code == "MAX_POSITIONS_PER_SYMBOL_REACHED"
    assert "Límite de posiciones para AUDUSD alcanzado" in reason


def test_check_exposure_limits_cluster_exposure_aud():
    # Tenemos AUDUSD abierto. Queremos abrir AUDCAD -> mismo cluster AUD
    open_positions = [{"symbol": "AUDUSD", "type": "BUY", "price_open": 0.6500, "sl": 0.6480}]

    ok, reason, code = clusters.check_exposure_limits(
        open_positions=open_positions,
        new_symbol="AUDCAD",
        new_risk_r=1.0,
        max_positions_per_cluster=1,
    )
    assert not ok
    assert code == "CLUSTER_EXPOSURE_REACHED"
    assert "cluster 'AUD'" in reason


def test_check_exposure_limits_cluster_exposure_usd():
    # Tenemos AUDUSD abierto. Queremos abrir EURUSD -> mismo cluster USD
    open_positions = [{"symbol": "AUDUSD", "type": "BUY", "price_open": 0.6500, "sl": 0.6480}]

    ok, reason, code = clusters.check_exposure_limits(
        open_positions=open_positions,
        new_symbol="EURUSD",
        new_risk_r=1.0,
        max_positions_per_cluster=1,
    )
    assert not ok
    assert code == "CLUSTER_EXPOSURE_REACHED"
    assert "cluster 'USD'" in reason


def test_check_exposure_limits_total_open_risk_cap():
    # EURUSD abierto (1.0R). Intentamos abrir GBPJPY (cluster GBP/JPY distinto de EUR/USD)
    # Pero el riesgo total proyectado sería 1.0R + 1.0R = 2.0R > max_total_open_risk_r (1.5R)
    open_positions = [{"symbol": "EURUSD", "type": "BUY", "price_open": 1.0800, "sl": 1.0780}]

    ok, reason, code = clusters.check_exposure_limits(
        open_positions=open_positions,
        new_symbol="GBPJPY",
        new_risk_r=1.0,
        max_positions_per_cluster=2,
        max_total_open_risk_r=1.5,
    )
    assert not ok
    assert code == "MAX_OPEN_RISK_REACHED"
    assert "Riesgo abierto total (2.00R) excedería el máximo permitido (1.50R)" in reason


def test_check_exposure_limits_allowed_when_previous_at_be():
    # EURUSD abierto pero YA en Break-Even -> 0.0R de riesgo vivo
    open_positions = [{"symbol": "EURUSD", "type": "BUY", "price_open": 1.0800, "sl": 1.0805}]

    ok, reason, code = clusters.check_exposure_limits(
        open_positions=open_positions,
        new_symbol="GBPJPY",
        new_risk_r=1.0,
        max_positions_per_cluster=2,
        max_total_open_risk_r=1.5,
    )
    assert ok
    assert reason is None
    assert code is None


# -- Dynamic Buffer Sizing (P1 - Audit #9) -----------------------------------


def test_dynamic_risk_pct_abundant_buffer():
    # Buffer $1,750, daily room $1,000 en cuenta $25,000 -> 0.5% nominal sin alteración
    risk = prop_rules.dynamic_risk_pct(
        base_risk_pct=0.5,
        buffer_remaining_usd=1750.0,
        daily_room_usd=1000.0,
        initial_capital=25000.0,
        divisor=2.0,
    )
    assert risk == 0.5


def test_dynamic_risk_pct_low_total_buffer():
    # Buffer restante de solo $100 -> tope = ($100 / (2 * 25,000)) * 100 = 0.20%
    risk = prop_rules.dynamic_risk_pct(
        base_risk_pct=0.5,
        buffer_remaining_usd=100.0,
        daily_room_usd=1000.0,
        initial_capital=25000.0,
        divisor=2.0,
    )
    assert risk == 0.20


def test_dynamic_risk_pct_low_daily_room():
    # Room diario de solo $80 -> tope = ($80 / (2 * 25,000)) * 100 = 0.16%
    risk = prop_rules.dynamic_risk_pct(
        base_risk_pct=0.5,
        buffer_remaining_usd=1750.0,
        daily_room_usd=80.0,
        initial_capital=25000.0,
        divisor=2.0,
    )
    assert risk == 0.16


def test_dynamic_risk_pct_exhausted_buffer():
    # Buffer agotado (0 o negativo) -> 0.0%
    risk = prop_rules.dynamic_risk_pct(
        base_risk_pct=0.5,
        buffer_remaining_usd=0.0,
        daily_room_usd=1000.0,
        initial_capital=25000.0,
        divisor=2.0,
    )
    assert risk == 0.0

