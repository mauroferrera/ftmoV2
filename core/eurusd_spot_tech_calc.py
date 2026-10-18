"""Cálculo técnico y motor determinista del Tribunal para EUR/USD usando velas (MT5 o genéricas).

Puro. Sin I/O. Sin red. Sin DB.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def sma(values: List[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    s = sum(values[-period:])
    return s / period


def ema(values: List[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_prev = sma(values[:period], period)
    if ema_prev is None:
        return None
    e = ema_prev
    for v in values[period:]:
        e = (v * k) + (e * (1 - k))
    return e


def _ema_series(values: List[float], period: int) -> List[float]:
    if period <= 0 or len(values) < period:
        return []
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    e = seed
    for v in values[period:]:
        e = (v * k) + (e * (1 - k))
        out.append(e)
    return out


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(len(values) - period, len(values)):
        ch = values[i] - values[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses += abs(ch)
    if losses == 0:
        return 100.0
    rs = gains / losses if losses > 0 else 0
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values: List[float]) -> Dict[str, Optional[float]]:
    ema12 = ema(values, 12)
    ema26 = ema(values, 26)
    if ema12 is None or ema26 is None:
        return {"macd": None, "signal": None, "hist": None}
    macd_line = ema12 - ema26

    # Serie MACD para calcular señal EMA(9) cuando hay suficientes velas (>= 35)
    signal_val: Optional[float] = None
    hist_val: Optional[float] = None
    if len(values) >= 35:
        s12 = _ema_series(values, 12)  # empieza en índice 11 (longitud N - 11)
        s26 = _ema_series(values, 26)  # empieza en índice 25 (longitud N - 25)
        offset = len(s12) - len(s26)
        macd_series = [s12[offset + i] - s26[i] for i in range(len(s26))]
        sig = ema(macd_series, 9)
        if sig is not None:
            signal_val = round(sig, 6)
            hist_val = round(macd_line - sig, 6)

    return {
        "macd": round(macd_line, 6),
        "signal": signal_val,
        "hist": hist_val,
    }


def calc_indicators(closes: List[float]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    s100 = sma(closes, 100)
    s200 = sma(closes, 200)
    r14 = rsi(closes, 14)
    out["sma20"] = round(s20, 5) if s20 is not None else None
    out["sma50"] = round(s50, 5) if s50 is not None else None
    out["sma100"] = round(s100, 5) if s100 is not None else None
    out["sma200"] = round(s200, 5) if s200 is not None else None
    out["rsi14"] = round(r14, 2) if r14 is not None else None
    out["macd"] = macd(closes)
    # precio vs medias
    if closes:
        c = closes[-1]
        out["price"] = c
        if out["sma50"] is not None:
            out["above_sma50"] = c > out["sma50"]
        if out["sma100"] is not None:
            out["above_sma100"] = c > out["sma100"]
    return out


def suggest_levels(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    pdh: Optional[float] = None,
    pdl: Optional[float] = None,
) -> Dict[str, List[float]]:
    if not closes:
        return {"supports": [], "resistances": []}
    c = closes[-1]
    supports: List[float] = []
    resistances: List[float] = []

    for window in (20, 60, 120):
        sub_l = lows[-window:] if len(lows) >= window else lows
        sub_h = highs[-window:] if len(highs) >= window else highs
        if sub_l:
            lv = round(min(sub_l), 5)
            if lv < c and lv not in supports:
                supports.append(lv)
        if sub_h:
            hv = round(max(sub_h), 5)
            if hv > c and hv not in resistances:
                resistances.append(hv)

    if pdl is not None:
        pdl_r = round(float(pdl), 5)
        if pdl_r < c and pdl_r not in supports:
            supports.append(pdl_r)
        elif pdl_r > c and pdl_r not in resistances:
            resistances.append(pdl_r)

    if pdh is not None:
        pdh_r = round(float(pdh), 5)
        if pdh_r > c and pdh_r not in resistances:
            resistances.append(pdh_r)
        elif pdh_r < c and pdh_r not in supports:
            supports.append(pdh_r)

    supports.sort(reverse=True)
    resistances.sort()
    return {"supports": supports[:3], "resistances": resistances[:3]}


def summarize_smc_and_structure(
    current_price: float,
    m15_indicators: Dict[str, Any],
    h1_indicators: Optional[Dict[str, Any]] = None,
    smc_analysis: Optional[Dict[str, Any]] = None,
    pdh: Optional[float] = None,
    pdl: Optional[float] = None,
    spread_points: Optional[float] = None,
) -> Dict[str, Any]:
    """Resume determinísticamente la microestructura MT5 + SMC (M15 y H1)."""
    patterns = (smc_analysis or {}).get("patterns") if isinstance(smc_analysis, dict) else {}
    patterns = patterns if isinstance(patterns, dict) else {}

    fvgs = patterns.get("fvgs") or []
    obs = patterns.get("order_blocks") or []
    sweeps = patterns.get("sweeps") or []

    bull_fvgs = [z for z in fvgs if z.get("type") == "BULLISH_FVG"]
    bear_fvgs = [z for z in fvgs if z.get("type") == "BEARISH_FVG"]
    bull_obs = [z for z in obs if z.get("type") == "BULLISH_OB"]
    bear_obs = [z for z in obs if z.get("type") == "BEARISH_OB"]

    recent_sweep: Optional[str] = None
    if sweeps:
        last_sw = sweeps[-1]
        sw_type = last_sw.get("type")
        lvl = last_sw.get("price_level")
        if sw_type == "PDH_SWEEP":
            recent_sweep = f"Barrido bajista de PDH ({lvl})"
        elif sw_type == "PDL_SWEEP":
            recent_sweep = f"Barrido alcista de PDL ({lvl})"

    # Sesgo M15
    m15_rsi = m15_indicators.get("rsi14")
    m15_above50 = m15_indicators.get("above_sma50")
    m15_above100 = m15_indicators.get("above_sma100")
    if m15_above50 is True and m15_above100 is True and (m15_rsi is None or m15_rsi >= 50.0):
        m15_trend = "alcista"
    elif m15_above50 is False and m15_above100 is False and (m15_rsi is None or m15_rsi <= 50.0):
        m15_trend = "bajista"
    else:
        m15_trend = "neutral"

    # Sesgo H1 (marco superior)
    h1_inds = h1_indicators or {}
    h1_rsi = h1_inds.get("rsi14")
    h1_above50 = h1_inds.get("above_sma50")
    if h1_above50 is True and (h1_rsi is None or h1_rsi >= 48.0):
        h1_trend = "alcista"
    elif h1_above50 is False and (h1_rsi is None or h1_rsi <= 52.0):
        h1_trend = "bajista"
    else:
        h1_trend = "neutral"

    dist_pdh_pips = round((float(pdh) - current_price) * 10000, 1) if pdh is not None else None
    dist_pdl_pips = round((current_price - float(pdl)) * 10000, 1) if pdl is not None else None

    nearest_bull_zone: Optional[str] = None
    for z in reversed(bull_obs + bull_fvgs):
        b, t = z.get("bottom"), z.get("top")
        if b is not None and t is not None and float(b) <= current_price + 0.0020:
            label = "Bullish OB" if "OB" in str(z.get("type")) else "Bullish FVG"
            nearest_bull_zone = f"{label} [{round(float(b), 5)}–{round(float(t), 5)}]"
            break

    nearest_bear_zone: Optional[str] = None
    for z in reversed(bear_obs + bear_fvgs):
        b, t = z.get("bottom"), z.get("top")
        if b is not None and t is not None and float(t) >= current_price - 0.0020:
            label = "Bearish OB" if "OB" in str(z.get("type")) else "Bearish FVG"
            nearest_bear_zone = f"{label} [{round(float(b), 5)}–{round(float(t), 5)}]"
            break

    parts = [
        f"M15 {m15_trend.upper()} | H1 {h1_trend.upper()}",
        f"Zonas vivas M15: {len(bull_fvgs)} FVG+, {len(bear_fvgs)} FVG-, {len(bull_obs)} OB+, {len(bear_obs)} OB-",
    ]
    if recent_sweep:
        parts.append(recent_sweep)
    if pdh is not None and pdl is not None:
        parts.append(f"PDH {round(float(pdh), 5)} / PDL {round(float(pdl), 5)}")

    return {
        "m15_trend": m15_trend,
        "h1_trend": h1_trend,
        "mtf_aligned": (m15_trend == h1_trend and m15_trend != "neutral"),
        "bull_fvgs_count": len(bull_fvgs),
        "bear_fvgs_count": len(bear_fvgs),
        "bull_obs_count": len(bull_obs),
        "bear_obs_count": len(bear_obs),
        "nearest_bull_zone": nearest_bull_zone,
        "nearest_bear_zone": nearest_bear_zone,
        "recent_sweep": recent_sweep,
        "pdh": round(float(pdh), 5) if pdh is not None else None,
        "pdl": round(float(pdl), 5) if pdl is not None else None,
        "dist_pdh_pips": dist_pdh_pips,
        "dist_pdl_pips": dist_pdl_pips,
        "spread_points": spread_points,
        "summary_text": " · ".join(parts),
    }


def build_deterministic_tribunal(
    current_price: float,
    m15_indicators: Dict[str, Any],
    levels: Dict[str, List[float]],
    smc_summary: Dict[str, Any],
    rss_items: Optional[List[Dict[str, Any]]] = None,
    calendar_events: Optional[List[Dict[str, Any]]] = None,
    grounding_mode: str = "deterministic_fallback",
) -> Dict[str, Any]:
    """Construye el debate Tesis (Fiscal) vs. Antítesis (Abogado del Diablo) -> Veredicto (Juez)
    de forma 100% determinista cuando Gemini no está disponible o para respaldo cuantitativo.
    """
    rss = rss_items or []
    cal = calendar_events or []

    m15_trend = smc_summary.get("m15_trend", "neutral")
    h1_trend = smc_summary.get("h1_trend", "neutral")
    rsi_val = m15_indicators.get("rsi14")
    rsi_num = float(rsi_val) if rsi_val is not None else 50.0
    macd_hist = (m15_indicators.get("macd") or {}).get("hist")

    supports = levels.get("supports") or []
    resistances = levels.get("resistances") or []
    sup_0 = supports[0] if supports else round(current_price - 0.0020, 5)
    res_0 = resistances[0] if resistances else round(current_price + 0.0020, 5)

    # 1. ROL FISCAL TÉCNICO (TESIS)
    bull_score = 0
    bear_score = 0
    if m15_trend == "alcista":
        bull_score += 2
    elif m15_trend == "bajista":
        bear_score += 2

    if h1_trend == "alcista":
        bull_score += 2
    elif h1_trend == "bajista":
        bear_score += 2

    if smc_summary.get("bull_obs_count", 0) + smc_summary.get("bull_fvgs_count", 0) > (
        smc_summary.get("bear_obs_count", 0) + smc_summary.get("bear_fvgs_count", 0)
    ):
        bull_score += 1
    elif smc_summary.get("bear_obs_count", 0) + smc_summary.get("bear_fvgs_count", 0) > (
        smc_summary.get("bull_obs_count", 0) + smc_summary.get("bull_fvgs_count", 0)
    ):
        bear_score += 1

    if macd_hist is not None:
        if macd_hist > 0:
            bull_score += 1
        elif macd_hist < 0:
            bear_score += 1

    if bull_score > bear_score:
        thesis_dir = "BUY"
        bias = "alcista"
        thesis_headline = f"Estructura compradora activa en {current_price} con respaldo SMC"
        thesis_points = [
            f"Precio ({current_price}) sostiene estructura M15 {m15_trend} y H1 {h1_trend} (RSI14: {rsi_num:.1f}).",
            f"Demanda institucional apoyada sobre soporte inmediato en {sup_0}"
            + (f" y zona {smc_summary['nearest_bull_zone']}." if smc_summary.get("nearest_bull_zone") else "."),
            f"Objetivo técnico hacia liquidez superior en {res_0}"
            + (f" / PDH {smc_summary['pdh']}." if smc_summary.get("pdh") else "."),
        ]
    elif bear_score > bull_score:
        thesis_dir = "SELL"
        bias = "bajista"
        thesis_headline = f"Presión vendedora dominante en {current_price} bajo medias clave"
        thesis_points = [
            f"Precio ({current_price}) presionado con estructura M15 {m15_trend} y H1 {h1_trend} (RSI14: {rsi_num:.1f}).",
            f"Oferta institucional defendiendo resistencia en {res_0}"
            + (f" y zona {smc_summary['nearest_bear_zone']}." if smc_summary.get("nearest_bear_zone") else "."),
            f"Proyección bajista hacia liquidez inferior en {sup_0}"
            + (f" / PDL {smc_summary['pdl']}." if smc_summary.get("pdl") else "."),
        ]
    else:
        thesis_dir = "NEUTRAL"
        bias = "neutral"
        thesis_headline = f"Equilibrio intradía en {current_price} dentro de rango {sup_0} – {res_0}"
        thesis_points = [
            f"Divergencia o compresión entre M15 ({m15_trend}) y H1 ({h1_trend}) con RSI14 en {rsi_num:.1f}.",
            f"Liquidez comprimida entre soporte {sup_0} y resistencia {res_0}.",
            "Sin desplazamiento institucional claro hasta que ocurra un sweep de extremos de sesión.",
        ]

    # 2. ROL ABOGADO DEL DIABLO (ANTÍTESIS)
    objections: List[str] = []
    penalty = 0

    if m15_trend != h1_trend and m15_trend != "neutral" and h1_trend != "neutral":
        objections.append(
            f"Choque de temporalidades: M15 apunta {m15_trend} pero H1 es {h1_trend}, elevando riesgo de falso rompimiento."
        )
        penalty += 20

    if thesis_dir == "BUY" and rsi_num >= 68.0:
        objections.append(f"RSI14 M15 agotado en {rsi_num:.1f} (cerca de sobrecompra): comprar aquí persigue el precio.")
        penalty += 15
    elif thesis_dir == "SELL" and rsi_num <= 32.0:
        objections.append(f"RSI14 M15 agotado en {rsi_num:.1f} (cerca de sobreventa): alto riesgo de short-squeeze.")
        penalty += 15

    if thesis_dir == "BUY" and smc_summary.get("nearest_bear_zone"):
        objections.append(f"Obstáculo SMC contrario encima del precio: {smc_summary['nearest_bear_zone']}.")
        penalty += 8
    elif thesis_dir == "SELL" and smc_summary.get("nearest_bull_zone"):
        objections.append(f"Bloque de demanda SMC debajo del precio: {smc_summary['nearest_bull_zone']}.")
        penalty += 8

    # Revisar calendario macro de alto impacto
    imminent_high: List[str] = []
    today_high: List[str] = []
    for ev in cal:
        imp = str(ev.get("impact") or "").lower()
        mins = ev.get("minutes_to_event")
        label = f"{ev.get('currency', '')} {ev.get('title', '')} ({ev.get('time_utc', 'hoy')})".strip()
        if imp == "high":
            today_high.append(label)
            if isinstance(mins, int) and -30 <= mins <= 90:
                imminent_high.append(label)

    if imminent_high:
        objections.append(
            f"ALERTA DE CALENDARIO INMINENTE (-30/+90 min): {', '.join(imminent_high[:2])}. Riesgo extremo de slippage y barrido bilateral."
        )
        penalty += 40
    elif today_high:
        objections.append(
            f"Eventos macro de Alto Impacto hoy en agenda: {', '.join(today_high[:2])}; pueden revertir la estructura técnica."
        )
        penalty += 12

    # Revisar titulares RSS en contra del sesgo
    opposing_rss = [
        it for it in rss
        if (bias == "alcista" and it.get("sentiment") == "bajista")
        or (bias == "bajista" and it.get("sentiment") == "alcista")
    ]
    if opposing_rss:
        sample_title = opposing_rss[0].get("title") or "flujo noticioso adverso"
        objections.append(
            f"Fricción fundamental en prensa ({len(opposing_rss)} titular(es) contrarios), ej.: \"{sample_title[:110]}\"."
        )
        penalty += 12

    if not objections:
        objections.append(
            f"Riesgo estándar de barrido de liquidez (stop-hunt) en torno a {sup_0 if thesis_dir == 'BUY' else res_0} durante aperturas de Londres/NY."
        )
        objections.append(
            "Posibles comentarios no programados de miembros del BCE o la Reserva Federal que alteren el diferencial de tasas."
        )

    macro_threats = (
        f"Eventos High-Impact hoy: {len(today_high)} ({len(imminent_high)} inminentes) · "
        f"Titulares RSS analizados: {len(rss)}."
    )

    # 3. ROL JUEZ CUANTITATIVO (VEREDICTO)
    base_conf = 50 + abs(bull_score - bear_score) * 7
    final_conf = max(35, min(88, base_conf - (penalty // 2)))

    if imminent_high:
        verdict_status = "VETO"
        rec_risk = 0.0
        ruling = (
            f"VETO TEMPORAL: Aunque el Fiscal observa sesgo {bias}, hay publicación macro de Alto Impacto inminente "
            f"({imminent_high[0]}). Se recomienda esperar 15-30 min tras el dato y confirmar absorción SMC."
        )
    elif thesis_dir == "NEUTRAL":
        verdict_status = "NEUTRAL"
        rec_risk = 0.25
        ruling = (
            f"DICTAMEN NEUTRAL: Sin ventaja direccional suficiente en {current_price}. "
            f"Esperar barrido y confirmación en extremos ({sup_0} o {res_0}) antes de comprometer riesgo."
        )
    elif penalty >= 20 or not smc_summary.get("mtf_aligned", False):
        verdict_status = "RIESGO_REDUCIDO"
        rec_risk = 0.25
        ruling = (
            f"APROBADO CON PRECAUCIÓN (Riesgo Reducido 0.25%): La tesis {thesis_dir} tiene respaldo en M15, "
            f"pero las objeciones del Abogado del Diablo justifican reducir exposición a la mitad del nominal FTMO."
        )
    else:
        verdict_status = "ALTA_CONFLUENCIA"
        rec_risk = 0.50
        ruling = (
            f"ALTA CONFLUENCIA ({thesis_dir} · Riesgo Nominal 0.50%): Alineación M15 + H1 + microestructura SMC "
            f"sin eventos macro bloqueantes inmediatos. Ejecutar únicamente tras confirmación en zona SMC con SL estricto."
        )

    if thesis_dir == "BUY":
        invalidation = f"Cierre de vela M15 por debajo de soporte {sup_0} invalida la tesis compradora."
    elif thesis_dir == "SELL":
        invalidation = f"Cierre de vela M15 por encima de resistencia {res_0} invalida la tesis vendedora."
    else:
        invalidation = f"Ruptura confirmada fuera del rango {sup_0} – {res_0} habilita nueva evaluación direccional."

    return {
        "grounding_mode": grounding_mode,
        "search_queries": [],
        "calendar_events_count": len(cal),
        "rss_headlines_count": len(rss),
        "thesis": {
            "role": "Fiscal Técnico (MT5 + SMC)",
            "direction": thesis_dir,
            "headline": thesis_headline,
            "points": thesis_points,
            "smc_summary": smc_summary.get("summary_text", ""),
        },
        "antithesis": {
            "role": "Abogado del Diablo (Web + Macro + Riesgo)",
            "headline": "Objeciones macroeconómicas, calendario y trampas de liquidez",
            "points": objections[:5],
            "macro_threats": macro_threats,
        },
        "verdict": {
            "role": "Juez Cuantitativo",
            "status": verdict_status,
            "bias": bias,
            "confidence": int(final_conf),
            "recommended_risk_pct": rec_risk,
            "ruling": ruling,
            "invalidation_condition": invalidation,
        },
    }
