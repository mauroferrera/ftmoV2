"""Tests unitarios puros para core.institutional_volume."""

import pytest
from core.institutional_volume import (
    analyze_futures_volume,
    calculate_volume_sma,
    classify_volume_activity,
    detect_absorption,
    detect_smt_divergence,
    evaluate_institutional_confluence,
)


def test_calculate_volume_sma():
    assert calculate_volume_sma([]) == []
    vols = [100.0, 200.0, 300.0]
    smas = calculate_volume_sma(vols, lookback=2)
    assert smas[0] == 100.0
    assert smas[1] == 150.0
    assert smas[2] == 250.0


def test_classify_volume_activity():
    tag, ratio, is_whale = classify_volume_activity(2500, 1000)
    assert tag == "PICO INSTITUCIONAL (BALLENAS)"
    assert ratio == 2.5
    assert is_whale is True

    tag, ratio, is_whale = classify_volume_activity(500, 1000)
    assert tag == "BAJO / SIN INTERÉS"
    assert ratio == 0.5
    assert is_whale is False

    tag, ratio, is_whale = classify_volume_activity(1100, 1000)
    assert tag == "NORMAL"
    assert is_whale is False


def test_detect_absorption_bullish():
    # Vela con rango 1.1100 a 1.1200 (100 pips).
    # Open 1.1160, Close 1.1180, Low 1.1100 -> lower_wick = 1.1160 - 1.1100 = 60 pips (60% del rango)
    candle = {
        "open": 1.1160,
        "high": 1.1200,
        "low": 1.1100,
        "close": 1.1180,
        "volume": 3000,
    }
    result = detect_absorption(candle, avg_volume=1500)
    assert result == "ABSORCION_COMPRADORA"


def test_detect_absorption_bearish():
    # Open 1.1140, Close 1.1120, High 1.1200 -> upper_wick = 1.1200 - 1.1140 = 60 pips (60% del rango)
    candle = {
        "open": 1.1140,
        "high": 1.1200,
        "low": 1.1100,
        "close": 1.1120,
        "volume": 2500,
    }
    result = detect_absorption(candle, avg_volume=1200)
    assert result == "ABSORCION_VENDEDORA"


def test_detect_absorption_none_when_low_volume():
    candle = {
        "open": 1.1160,
        "high": 1.1200,
        "low": 1.1100,
        "close": 1.1180,
        "volume": 1000,  # Below 1.4x of 1500
    }
    assert detect_absorption(candle, avg_volume=1500) is None


def test_analyze_futures_volume():
    candles = [
        {"time": f"2026-10-31 10:{i:02d}", "open": 1.11, "high": 1.12, "low": 1.10, "close": 1.115, "volume": 1000}
        for i in range(10)
    ]
    # Add a whale candle at the end
    candles.append({
        "time": "2026-10-31 10:15",
        "open": 1.116,
        "high": 1.120,
        "low": 1.110,
        "close": 1.118,
        "volume": 2500,
    })
    res = analyze_futures_volume(candles, lookback=10)
    assert res["available"] is True
    assert res["last_volume"] == 2500.0
    assert res["avg_volume"] == 1000.0
    assert res["vol_ratio"] == 2.5
    assert res["whale_detected"] is True
    assert res["absorption"] == "ABSORCION_COMPRADORA"


def test_detect_smt_divergence_bullish():
    # Asset defended Lows while Benchmark broke Highs
    asset = [
        {"high": 1.1200, "low": 1.1150},
        {"high": 1.1210, "low": 1.1160},
        {"high": 1.1205, "low": 1.1155},  # Low 1.1155 > 1.1150 (Did not break prior low)
    ]
    bm = [
        {"high": 101.50, "low": 101.00},
        {"high": 101.60, "low": 101.10},
        {"high": 101.80, "low": 101.20},  # High 101.80 > 101.60 (Broke prior high)
    ]
    res = detect_smt_divergence(asset, bm, lookback_bars=3)
    assert res["status"] == "BULLISH_SMT"
    assert res["bias"] == "BUY"


def test_detect_smt_divergence_direct_correlation():
    # Asset (e.g. USDCAD) with direct correlation to benchmark DXY (inverse=False)
    # Benchmark made HH, but Asset failed to make HH -> BEARISH_SMT (selling USD)
    asset = [
        {"high": 1.3800, "low": 1.3750},
        {"high": 1.3820, "low": 1.3760},
        {"high": 1.3810, "low": 1.3755},  # Did not break prior high 1.3820
    ]
    bm = [
        {"high": 104.00, "low": 103.50},
        {"high": 104.20, "low": 103.60},
        {"high": 104.50, "low": 103.70},  # Broke prior high
    ]
    res = detect_smt_divergence(asset, bm, lookback_bars=3, inverse=False)
    assert res["status"] == "BEARISH_SMT"
    assert res["bias"] == "SELL"


def test_evaluate_institutional_confluence_inverted_future():
    # Inverted future (e.g. 6J1! buying Yen means USDJPY is SELL)
    futures = {
        "available": True,
        "whale_detected": True,
        "vol_ratio": 2.2,
        "absorption": "ABSORCION_COMPRADORA",
    }
    # For inverted future, ABSORCION_COMPRADORA aligns with SELL
    res_sell = evaluate_institutional_confluence("SELL", futures, inverted_future=True)
    assert res_sell["status"] == "ALTA_CONFLUENCIA_BALLENAS"
    assert res_sell["aligned"] is True

    # For inverted future, ABSORCION_COMPRADORA opposes BUY
    res_buy = evaluate_institutional_confluence("BUY", futures, inverted_future=True)
    assert res_buy["status"] == "ALERTA_CONTRA_BALLENAS"
    assert res_buy["aligned"] is False


def test_analyze_futures_volume_prefer_closed_ignores_partial_forming_bar():
    # 10 velas previas con volumen 1930, última cerrada (candles[-2]) con 2886 contratos (2.89K),
    # y vela recién abierta en formación (candles[-1]) con apenas 703 contratos.
    candles = [
        {"time": f"2026-11-03 10:{i:02d}", "open": 1.119, "high": 1.120, "low": 1.118, "close": 1.1195, "volume": 1930}
        for i in range(10)
    ]
    candles.append({
        "time": "2026-11-03 12:00",
        "open": 1.1195,
        "high": 1.1202,
        "low": 1.1191,
        "close": 1.1197,
        "volume": 2886,
    })
    candles.append({
        "time": "2026-11-03 12:15",
        "open": 1.1197,
        "high": 1.1199,
        "low": 1.1195,
        "close": 1.1196,
        "volume": 703,
    })

    # Sin prefer_closed penalizaría la vela recién abierta (703 / 2016.9 = 0.35x)
    res_forming = analyze_futures_volume(candles, lookback=10, prefer_closed=False)
    assert res_forming["last_volume"] == 703.0
    assert res_forming["vol_ratio"] < 0.65

    # Con prefer_closed=True toma la vela cerrada de 2886 contratos (1.5x del promedio)
    res_closed = analyze_futures_volume(candles, lookback=10, prefer_closed=True)
    assert res_closed["last_volume"] == 2886.0
    assert res_closed["closed_volume"] == 2886.0
    assert res_closed["forming_volume"] == 703.0
    assert res_closed["vol_ratio"] == 1.5
    assert res_closed["vol_tag"] == "NORMAL"


def test_evaluate_institutional_confluence_realtime_zero_delay_boost():
    # CME diferido muestra volumen normal (1.49x), pero el feed OANDA en tiempo real (0 delay)
    # detecta absorción compradora ahora mismo.
    futures = {
        "available": True,
        "last_volume": 2886.0,
        "closed_volume": 2886.0,
        "forming_volume": 703.0,
        "whale_detected": False,
        "vol_ratio": 1.49,
        "absorption": None,
    }
    realtime = {
        "available": True,
        "whale_detected": True,
        "vol_ratio": 2.35,
        "absorption": "ABSORCION_COMPRADORA",
    }
    res = evaluate_institutional_confluence("BUY", futures, realtime_analysis=realtime)
    assert res["status"] == "ALTA_CONFLUENCIA_BALLENAS"
    assert res["aligned"] is True
    # Pureza de unidades (D-043): la ballena de contratos es False porque CME no vio ballena,
    # pero el pico en vivo y absorción quedan registrados honestamente en sus campos correspondientes.
    assert res["whale_detected"] is False
    assert res["rt_whale"] is True
    assert res["vol_ratio"] == 1.49
    assert res["vol_ratio_cme"] == 1.49
    assert res["rt_vol_ratio"] == 2.35
    assert res["evidence_source"] == "CME+SPOT"
    assert res["last_volume"] == 2886.0


def test_evaluate_institutional_confluence_cme_unavailable_spot_low():
    # Si CME no responde, sembramos desde el feed spot y detectamos volumen bajo (0.5x)
    futures = {"available": False}
    realtime = {"available": True, "vol_ratio": 0.5, "whale_detected": False}
    res = evaluate_institutional_confluence("BUY", futures, realtime_analysis=realtime)
    assert res["status"] == "BAJO_VOLUMEN_SPOT"
    assert res["vol_ratio"] == 0.5
    assert res["vol_ratio_cme"] is None
    assert res["rt_vol_ratio"] == 0.5
    assert res["evidence_source"] == "SPOT"
    assert "Volumen raquítico (0.5x) en SPOT" in res["detail"]


def test_evaluate_institutional_confluence_absorption_with_divergent_smt():
    # En XAUUSD: absorción compradora en BUY pero SMT bajista (BEARISH_SMT)
    futures = {
        "available": True,
        "last_volume": 4273.0,
        "closed_volume": 4273.0,
        "forming_volume": 2227.0,
        "vol_ratio": 2.0,
        "whale_detected": True,
        "absorption": "ABSORCION_COMPRADORA",
    }
    smt = {"status": "BEARISH_SMT", "bias": "SELL"}
    res = evaluate_institutional_confluence("BUY", futures, smt_analysis=smt)
    assert res["status"] == "ALTA_CONFLUENCIA_BALLENAS"
    assert res["aligned"] is True
    # El detail debe ser honesto: menciona absorción y contratos, y alerta sobre la divergencia de SMT
    assert "absorción ABSORCION_COMPRADORA en CME" in res["detail"]
    assert "Atención: SMT divergente BEARISH_SMT" in res["detail"]
    assert "huella de absorción/SMT" not in res["detail"]



