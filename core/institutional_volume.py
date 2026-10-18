"""Cálculos cuantitativos puros de volumen institucional y confluencia macro.

Reglas de pureza:
- 100% determinista, sin I/O, sin red, sin base de datos ni librerías externas.
- Recibe estructuras de datos normalizadas (velas, series de volumen, extremos).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def calculate_volume_sma(volumes: List[float], lookback: int = 10) -> List[float]:
    """Calcula la media móvil simple de volumen para una serie."""
    if not volumes:
        return []
    smas: List[float] = []
    for i in range(len(volumes)):
        start = max(0, i - lookback + 1)
        sub = volumes[start : i + 1]
        smas.append(sum(sub) / len(sub) if sub else 0.0)
    return smas


def classify_volume_activity(
    volume: float, avg_volume: float, spike_mult: float = 1.75, low_mult: float = 0.65
) -> Tuple[str, float, bool]:
    """
    Clasifica el nivel de actividad de volumen en contratos reales.
    Retorna (tag, ratio, is_whale).
    """
    if avg_volume <= 0:
        return "NORMAL", 1.0, False
    ratio = round(volume / avg_volume, 2)
    if ratio >= spike_mult:
        return "PICO INSTITUCIONAL (BALLENAS)", ratio, True
    if ratio <= low_mult:
        return "BAJO / SIN INTERÉS", ratio, False
    return "NORMAL", ratio, False


def detect_absorption(
    candle: Dict[str, Any],
    avg_volume: float,
    wick_threshold: float = 0.40,
    spike_mult: float = 1.40,
) -> Optional[str]:
    """
    Detecta absorción institucional en una vela de futuros:
    - Ocurre cuando el volumen es significativamente alto (> spike_mult * avg_volume)
    - Y la mecha (rechazo) representa al menos el `wick_threshold` del rango total.
    """
    total_range = float(candle.get("high", 0.0)) - float(candle.get("low", 0.0))
    if total_range <= 0:
        return None

    vol = float(candle.get("volume", 0.0))
    if avg_volume > 0 and (vol / avg_volume) < spike_mult:
        return None

    op = float(candle.get("open", 0.0))
    cl = float(candle.get("close", 0.0))
    hi = float(candle.get("high", 0.0))
    lo = float(candle.get("low", 0.0))

    lower_wick = min(op, cl) - lo
    upper_wick = hi - max(op, cl)

    if (lower_wick / total_range) >= wick_threshold:
        return "ABSORCION_COMPRADORA"
    if (upper_wick / total_range) >= wick_threshold:
        return "ABSORCION_VENDEDORA"
    return None


def analyze_futures_volume(
    candles: List[Dict[str, Any]], lookback: int = 10
) -> Dict[str, Any]:
    """
    Analiza una serie de velas de futuros (ej. CME:6E1! o COMEX:GC1!).
    Devuelve métricas cuantitativas de la última vela y de la serie reciente.
    """
    if not candles:
        return {
            "available": False,
            "last_volume": 0.0,
            "avg_volume": 0.0,
            "vol_ratio": 1.0,
            "vol_tag": "SIN_DATOS",
            "whale_detected": False,
            "absorption": None,
        }

    vols = [float(c.get("volume", 0.0)) for c in candles]
    last_candle = candles[-1]
    last_vol = float(last_candle.get("volume", 0.0))

    # Promedio previo a la última vela (para no contaminar con la vela en evaluación)
    prior_vols = vols[:-1] if len(vols) > 1 else vols
    avg_vol = (sum(prior_vols[-lookback:]) / min(len(prior_vols), lookback)) if prior_vols else last_vol

    tag, ratio, is_whale = classify_volume_activity(last_vol, avg_vol)
    absorption = detect_absorption(last_candle, avg_vol)

    return {
        "available": True,
        "time": last_candle.get("time"),
        "last_volume": round(last_vol, 1),
        "avg_volume": round(avg_vol, 1),
        "vol_ratio": ratio,
        "vol_tag": tag,
        "whale_detected": is_whale,
        "absorption": absorption,
    }


def detect_smt_divergence(
    asset_candles: List[Dict[str, Any]],
    benchmark_candles: List[Dict[str, Any]],
    lookback_bars: int = 4,
    inverse: bool = True,
) -> Dict[str, Any]:
    """
    Evalúa divergencia SMT (Smart Money Technique) entre un activo y su benchmark.
    - inverse=True: correlación inversa (ej. EUR/USD, GBP/USD, AUD/USD, XAU/USD vs DXY).
    - inverse=False: correlación directa positiva (ej. USD/CAD, USD/JPY, USD/CHF vs DXY).
    """
    if len(asset_candles) < lookback_bars or len(benchmark_candles) < lookback_bars:
        return {"status": "INSUFFICIENT_DATA", "detail": "Velas insuficientes para SMT"}

    recent_asset = asset_candles[-lookback_bars:]
    recent_bm = benchmark_candles[-lookback_bars:]

    # Extremos en la ventana
    a_last = recent_asset[-1]
    a_priors = recent_asset[:-1]
    bm_last = recent_bm[-1]
    bm_priors = recent_bm[:-1]

    a_highest_prior = max(float(c.get("high", 0.0)) for c in a_priors)
    a_lowest_prior = min(float(c.get("low", 0.0)) for c in a_priors)

    bm_highest_prior = max(float(c.get("high", 0.0)) for c in bm_priors)
    bm_lowest_prior = min(float(c.get("low", 0.0)) for c in bm_priors)

    a_made_hh = float(a_last.get("high", 0.0)) > a_highest_prior
    a_made_ll = float(a_last.get("low", 0.0)) < a_lowest_prior

    bm_made_hh = float(bm_last.get("high", 0.0)) > bm_highest_prior
    bm_made_ll = float(bm_last.get("low", 0.0)) < bm_lowest_prior

    if inverse:
        if bm_made_hh and not a_made_ll:
            return {
                "status": "BULLISH_SMT",
                "bias": "BUY",
                "detail": "Benchmark hizo nuevo Máximo pero el activo defendió sus Mínimos (Acumulación institucional)",
            }
        if not bm_made_hh and a_made_ll:
            return {
                "status": "BULLISH_SMT",
                "bias": "BUY",
                "detail": "Activo barrió Mínimos en solitario sin confirmación del Benchmark (Trampa de liquidez)",
            }
        if bm_made_ll and not a_made_hh:
            return {
                "status": "BEARISH_SMT",
                "bias": "SELL",
                "detail": "Benchmark hizo nuevo Mínimo pero el activo no logró romper Máximos (Distribución institucional)",
            }
        if not bm_made_ll and a_made_hh:
            return {
                "status": "BEARISH_SMT",
                "bias": "SELL",
                "detail": "Activo barrió Máximos sin confirmación del Benchmark (Caza de stops / Trampa alcista)",
            }
    else:
        # Correlación directa: activo y benchmark deberían hacer los mismos extremos
        if bm_made_hh and not a_made_hh:
            return {
                "status": "BEARISH_SMT",
                "bias": "SELL",
                "detail": "Benchmark hizo nuevo Máximo pero el activo no acompañó (Debilidad institucional relativa)",
            }
        if not bm_made_hh and a_made_hh:
            return {
                "status": "BEARISH_SMT",
                "bias": "SELL",
                "detail": "Activo barrió Máximos en solitario sin confirmación del Benchmark (Caza de stops / Trampa alcista)",
            }
        if bm_made_ll and not a_made_ll:
            return {
                "status": "BULLISH_SMT",
                "bias": "BUY",
                "detail": "Benchmark hizo nuevo Mínimo pero el activo defendió Mínimos (Fuerza institucional relativa)",
            }
        if not bm_made_ll and a_made_ll:
            return {
                "status": "BULLISH_SMT",
                "bias": "BUY",
                "detail": "Activo barrió Mínimos en solitario sin confirmación del Benchmark (Trampa bajista / Absorción)",
            }

    return {
        "status": "ALINEADO",
        "bias": "NEUTRAL",
        "detail": "Estructuras de mercado sincronizadas normalmente",
    }


def evaluate_institutional_confluence(
    setup_direction: str,
    futures_analysis: Dict[str, Any],
    smt_analysis: Optional[Dict[str, Any]] = None,
    inverted_future: bool = False,
) -> Dict[str, Any]:
    """
    Emite un dictamen de confluencia institucional determinista para un setup técnico.
    `inverted_future=True` aplica cuando el futuro CME cotiza Divisa/USD pero el par spot es USD/Divisa
    (ej. 6C1! para USDCAD, 6J1! para USDJPY, 6S1! para USDCHF).
    """
    if not futures_analysis.get("available"):
        return {
            "aligned": None,
            "status": "SIN_DATOS",
            "whale_detected": False,
            "vol_ratio": 1.0,
            "absorption": None,
            "detail": "Datos de futuros CME no disponibles",
        }

    direction = (setup_direction or "").upper()
    whale = bool(futures_analysis.get("whale_detected"))
    ratio = float(futures_analysis.get("vol_ratio", 1.0))
    abs_type = futures_analysis.get("absorption")

    # Evaluación de absorción (respetando si el futuro está invertido respecto al par spot)
    absorption_alignment = None
    if abs_type == "ABSORCION_COMPRADORA":
        absorption_alignment = (direction == ("SELL" if inverted_future else "BUY"))
    elif abs_type == "ABSORCION_VENDEDORA":
        absorption_alignment = (direction == ("BUY" if inverted_future else "SELL"))

    # Evaluación de SMT
    smt_status = (smt_analysis or {}).get("status", "ALINEADO")
    smt_bias = (smt_analysis or {}).get("bias")
    smt_alignment = None
    if smt_bias in ("BUY", "SELL"):
        smt_alignment = (direction == smt_bias)

    # Veredicto integrado
    if absorption_alignment is True or (whale and smt_alignment is True):
        status = "ALTA_CONFLUENCIA_BALLENAS"
        aligned = True
        detail = f"Volumen CME {ratio:.1f}x respaldando la dirección {direction} con huella institucional"
    elif absorption_alignment is False:
        status = "ALERTA_CONTRA_BALLENAS"
        aligned = False
        detail = f"Absorción contraria detectada en CME ({abs_type})"
    elif ratio <= 0.65:
        status = "BAJO_VOLUMEN_CME"
        aligned = None
        detail = f"Volumen raquítico ({ratio:.1f}x) en futuros CME; riesgo de falso movimiento"
    elif whale:
        status = "BALLENAS_ACTIVAS"
        aligned = True
        detail = f"Pico de contratos en CME ({ratio:.1f}x)"
    else:
        status = "VOLUMEN_ESTANDAR"
        aligned = None
        detail = f"Actividad normal en CME ({ratio:.1f}x)"

    return {
        "aligned": aligned,
        "status": status,
        "whale_detected": whale,
        "vol_ratio": ratio,
        "absorption": abs_type,
        "smt_status": smt_status,
        "detail": detail,
    }


__all__ = [
    "analyze_futures_volume",
    "calculate_volume_sma",
    "classify_volume_activity",
    "detect_absorption",
    "detect_smt_divergence",
    "evaluate_institutional_confluence",
]

