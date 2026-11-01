"""Pruebas unitarias de indicadores y microestructura pura (M21)."""

from __future__ import annotations

import pytest

from core.indicators import calculate_atr, structural_sl_and_tp


def test_atr_velas_vacias():
    assert calculate_atr([]) == 0.0


def test_atr_una_vela():
    candles = [{"high": 1.1000, "low": 1.0950, "close": 1.0980}]
    assert calculate_atr(candles) == pytest.approx(0.0050)


def test_atr_calculo_correcto_con_gaps():
    # Serie de 3 velas con gap entre vela 1 y 2
    candles = [
        {"high": 1.1000, "low": 1.0900, "close": 1.0950},  # TR0 = 0.0100
        {"high": 1.1200, "low": 1.1100, "close": 1.1150},  # H=1.12, L=1.11, PrevC=1.095 -> TR1 = 1.12 - 1.095 = 0.0250
        {"high": 1.1180, "low": 1.1050, "close": 1.1100},  # H=1.118, L=1.105, PrevC=1.115 -> TR2 = 1.118 - 1.105 = 0.0130
    ]
    # Promedio sobre 3 velas
    res = calculate_atr(candles, period=3)
    esperado = (0.0100 + 0.0250 + 0.0130) / 3.0
    assert res == pytest.approx(esperado, abs=1e-5)


def test_sl_estructural_buy_con_zona_y_atr():
    # Entrada en 1.0850, distal en 1.0840 (10 pips)
    # ATR(14) = 0.0020 (20 pips) -> buffer = 0.15 * 0.0020 = 0.00030 (3 pips)
    # sl_raw = 1.0840 - 0.00030 = 1.08370
    # sl_dist = 1.0850 - 1.08370 = 0.00130 (13 pips)
    # sl = 1.08370
    plan = structural_sl_and_tp(
        entry=1.0850,
        direction="BUY",
        distal=1.0840,
        atr=0.0020,
        spread=0.00010,
        sl_min=0.00100,  # 10 pips min
        tp_ratio=2.0,
        digits=5,
    )
    assert plan["is_structural"] is True
    assert plan["sl"] == pytest.approx(1.08370)
    assert plan["sl_distance"] == pytest.approx(0.00130)
    assert plan["tp"] == pytest.approx(1.0850 + 0.00130 * 2.0)  # 1.08760
    assert plan["buffer"] == pytest.approx(0.00030)


def test_sl_estructural_sell_con_zona_y_atr():
    # Entrada en 1.0850, distal en 1.0860
    # buffer = 0.15 * 0.0020 = 0.00030
    # sl = 1.0860 + 0.00030 = 1.08630
    plan = structural_sl_and_tp(
        entry=1.0850,
        direction="SELL",
        distal=1.0860,
        atr=0.0020,
        spread=0.00010,
        sl_min=0.00100,
        tp_ratio=2.0,
        digits=5,
    )
    assert plan["is_structural"] is True
    assert plan["sl"] == pytest.approx(1.08630)
    assert plan["sl_distance"] == pytest.approx(0.00130)
    assert plan["tp"] == pytest.approx(1.0850 - 0.00130 * 2.0)  # 1.08240


def test_sl_estructural_clamp_sl_min():
    # Zona muy estrecha: distal a 3 pips
    # sl_min = 10 pips (0.00100)
    plan = structural_sl_and_tp(
        entry=1.0850,
        direction="BUY",
        distal=1.0847,
        atr=0.0005,
        sl_min=0.00100,
        digits=5,
    )
    assert plan["sl_distance"] == pytest.approx(0.00100)
    assert plan["sl"] == pytest.approx(1.08400)


def test_tp_iman_de_liquidez_aplicado():
    # Inyección de imán mayor en 1.0890 (a 40 pips de 1.0850)
    # SL distance = 13 pips -> 2R = 26 pips. Imán a 40 pips cumple >= 2R.
    plan = structural_sl_and_tp(
        entry=1.0850,
        direction="BUY",
        distal=1.0840,
        atr=0.0020,
        target_magnet=1.0890,
        digits=5,
    )
    assert plan["magnet_applied"] is True
    assert plan["tp"] == pytest.approx(1.0890)
    assert plan["tp_distance"] == pytest.approx(0.0040)


def test_fallback_sin_zona_usa_1_5_atr():
    # Sin zona distal
    # ATR = 0.0020 -> SL distance = 1.5 * 0.0020 = 0.0030 (30 pips)
    plan = structural_sl_and_tp(
        entry=1.0850,
        direction="BUY",
        distal=None,
        atr=0.0020,
        sl_min=0.0010,
        digits=5,
    )
    assert plan["is_structural"] is False
    assert plan["sl_distance"] == pytest.approx(0.00300)
    assert plan["sl"] == pytest.approx(1.08200)

