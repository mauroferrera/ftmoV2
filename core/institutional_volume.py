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
    candles: List[Dict[str, Any]],
    lookback: int = 10,
    prefer_closed: bool = False,
) -> Dict[str, Any]:
    """
    Analiza una serie de velas de futuros o liquidez institucional (ej. CME:6E1! u OANDA:EURUSD).
    Devuelve métricas cuantitativas de la vela de referencia y de la serie reciente.

    Si `prefer_closed=True` y hay al menos 3 velas, toma como referencia la última
    vela CERRADA (`candles[-2]`) para no penalizar una vela que recién acaba de abrir,
    SALVO que la vela en formación (`candles[-1]`) ya supere el volumen promedio o
    muestre absorción/pico institucional en curso.
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
    forming_candle = candles[-1]
    forming_vol = float(forming_candle.get("volume", 0.0))

    if prefer_closed and len(candles) >= 3:
        closed_candle = candles[-2]
        closed_vol = float(closed_candle.get("volume", 0.0))
        prior_vols = vols[:-2]
        avg_vol = (
            (sum(prior_vols[-lookback:]) / min(len(prior_vols), lookback))
            if prior_vols
            else closed_vol
        )
        # Si la vela en formación ya supera el promedio o tiene absorción activa, usamos la viva
        forming_abs = detect_absorption(forming_candle, avg_vol)
        if forming_vol >= max(avg_vol, closed_vol) or forming_abs is not None:
            ref_candle = forming_candle
            ref_vol = forming_vol
        else:
            ref_candle = closed_candle
            ref_vol = closed_vol
    else:
        ref_candle = forming_candle
        ref_vol = forming_vol
        closed_vol = float(candles[-2].get("volume", 0.0)) if len(candles) >= 2 else ref_vol
        prior_vols = vols[:-1] if len(vols) > 1 else vols
        avg_vol = (
            (sum(prior_vols[-lookback:]) / min(len(prior_vols), lookback))
            if prior_vols
            else ref_vol
        )

    tag, ratio, is_whale = classify_volume_activity(ref_vol, avg_vol)
    absorption = detect_absorption(ref_candle, avg_vol)

    return {
        "available": True,
        "time": ref_candle.get("time"),
        "last_volume": round(ref_vol, 1),
        "closed_volume": round(closed_vol, 1),
        "forming_volume": round(forming_vol, 1),
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
    realtime_analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Emite un dictamen de confluencia institucional determinista para un setup técnico.
    `inverted_future=True` aplica cuando el futuro CME cotiza Divisa/USD pero el par spot es USD/Divisa
    (ej. 6C1! para USDCAD, 6J1! para USDJPY, 6S1! para USDCHF).

    Trazabilidad y pureza de unidades (D-043):
    - `futures_analysis` refleja contratos reales de futuros CME/COMEX.
    - `realtime_analysis` refleja tick-volume interbancario de spot (ej. OANDA:<SYM>, 0s delay).
    - El volumen de ticks de spot NO puede crear `whale_detected = True` por sí solo ni sobreescribir
      el ratio de contratos de CME. Actúa como confirmación en vivo y cobertura del delay de 10 min.
    """
    cme_avail = bool(futures_analysis.get("available"))
    rt_avail = bool((realtime_analysis or {}).get("available"))

    if not cme_avail and not rt_avail:
        return {
            "aligned": None,
            "status": "SIN_DATOS",
            "whale_detected": False,
            "vol_ratio": 1.0,
            "vol_ratio_cme": None,
            "rt_vol_ratio": None,
            "last_volume": 0.0,
            "closed_volume": 0.0,
            "forming_volume": 0.0,
            "evidence_source": "NONE",
            "absorption": None,
            "smt_status": "SIN_DATOS",
            "detail": "Sin datos de volumen institucional ni spot",
        }

    direction = (setup_direction or "").upper()

    # Datos CME (contratos regulados)
    cme_whale = bool(futures_analysis.get("whale_detected")) if cme_avail else False
    cme_ratio = float(futures_analysis.get("vol_ratio") or 1.0) if cme_avail else None
    cme_abs = futures_analysis.get("absorption") if cme_avail else None
    last_vol = float(futures_analysis.get("last_volume") or 0.0) if cme_avail else 0.0
    closed_vol = float(futures_analysis.get("closed_volume") or last_vol) if cme_avail else 0.0
    forming_vol = float(futures_analysis.get("forming_volume") or last_vol) if cme_avail else 0.0

    # Datos Spot (ticks 0s delay)
    rt_ratio: Optional[float] = None
    rt_whale = False
    rt_abs = None
    if rt_avail and realtime_analysis:
        rt_ratio = float(realtime_analysis.get("vol_ratio") or 1.0)
        rt_whale = bool(realtime_analysis.get("whale_detected"))
        rt_abs = realtime_analysis.get("absorption")

    # Identificación honesta de la fuente de evidencia
    if cme_avail and rt_avail:
        evidence_source = "CME+SPOT"
    elif cme_avail:
        evidence_source = "CME"
    else:
        evidence_source = "SPOT"

    # Una ballena institucional en futuros proviene estrictamente de contratos CME
    whale = cme_whale

    # Sembrado del ratio principal: CME prioritario; si CME está caído, usamos Spot
    if cme_avail:
        main_ratio = cme_ratio if cme_ratio is not None else 1.0
    else:
        main_ratio = rt_ratio if rt_ratio is not None else 1.0

    # Evaluación de absorción institucional
    cme_abs_alignment = None
    if cme_abs == "ABSORCION_COMPRADORA":
        cme_abs_alignment = (direction == ("SELL" if inverted_future else "BUY"))
    elif cme_abs == "ABSORCION_VENDEDORA":
        cme_abs_alignment = (direction == ("BUY" if inverted_future else "SELL"))

    rt_abs_alignment = None
    if rt_abs == "ABSORCION_COMPRADORA":
        rt_abs_alignment = (direction == "BUY")
    elif rt_abs == "ABSORCION_VENDEDORA":
        rt_abs_alignment = (direction == "SELL")

    # Selección de absorción activa: CME tiene prioridad; Spot cubre la ventana en vivo si CME no vio absorción
    if cme_abs_alignment is not None:
        abs_type = cme_abs
        absorption_alignment = cme_abs_alignment
        abs_source = "CME"
    elif rt_abs_alignment is not None:
        abs_type = rt_abs
        absorption_alignment = rt_abs_alignment
        abs_source = "SPOT"
    else:
        abs_type = None
        absorption_alignment = None
        abs_source = None

    # Evaluación de SMT
    smt_status = (smt_analysis or {}).get("status", "ALINEADO")
    smt_bias = (smt_analysis or {}).get("bias")
    smt_alignment = None
    if smt_bias in ("BUY", "SELL"):
        smt_alignment = (direction == smt_bias)

    # Veredicto integrado con redacción honesta del detalle
    if absorption_alignment is False:
        status = "ALERTA_CONTRA_BALLENAS"
        aligned = False
        detail = f"Absorción contraria en {abs_source} ({abs_type}) contra {direction}"
        if smt_alignment is False:
            detail += f"; además SMT adverso ({smt_status})"
    elif absorption_alignment is True or (whale and smt_alignment is True):
        status = "ALTA_CONFLUENCIA_BALLENAS"
        aligned = True
        evidences = []
        if absorption_alignment is True:
            evidences.append(f"absorción {abs_type} en {abs_source}")
        if whale and cme_ratio is not None:
            evidences.append(f"pico de contratos CME ({cme_ratio:.1f}x)")
        elif rt_whale and rt_ratio is not None:
            evidences.append(f"pico de ticks en vivo ({rt_ratio:.1f}x)")
        elif main_ratio >= 1.5:
            evidences.append(f"volumen elevado ({main_ratio:.1f}x)")
        if smt_alignment is True:
            evidences.append(f"SMT confirmando ({smt_status})")

        detail = f"Alta confluencia institucional para {direction}: " + ", ".join(evidences)
        if smt_alignment is False:
            detail += f" (Atención: SMT divergente {smt_status})"
    elif main_ratio <= 0.65:
        status = "BAJO_VOLUMEN_CME" if cme_avail else "BAJO_VOLUMEN_SPOT"
        aligned = None
        detail = f"Volumen raquítico ({main_ratio:.1f}x) en {evidence_source}; riesgo de falso movimiento"
        if smt_alignment is False:
            detail += f" (y SMT divergente {smt_status})"
    elif whale:
        status = "BALLENAS_ACTIVAS"
        aligned = True
        cme_str = f" ({cme_ratio:.1f}x)" if cme_ratio is not None else ""
        detail = f"Pico de contratos en CME{cme_str}"
        if smt_alignment is True:
            detail += f" con SMT favorable ({smt_status})"
        elif smt_alignment is False:
            detail += f" (Atención: SMT divergente {smt_status})"
    else:
        status = "VOLUMEN_ESTANDAR"
        aligned = None
        detail = f"Actividad institucional normal ({main_ratio:.1f}x en {evidence_source})"
        if rt_whale and rt_ratio is not None:
            detail += f" con pico de ticks en spot ({rt_ratio:.1f}x)"
        if smt_alignment is True:
            detail += f" con SMT favorable ({smt_status})"
        elif smt_alignment is False:
            detail += f" con SMT divergente ({smt_status})"

    return {
        "aligned": aligned,
        "status": status,
        "whale_detected": whale,
        "vol_ratio": main_ratio,
        "vol_ratio_cme": cme_ratio,
        "rt_vol_ratio": rt_ratio,
        "evidence_source": evidence_source,
        "last_volume": last_vol,
        "closed_volume": closed_vol,
        "forming_volume": forming_vol,
        "rt_whale": rt_whale,
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

