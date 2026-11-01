"""Motor de detección de patrones SMC: Fair Value Gaps, Order Blocks, Sweeps.

Algoritmos puros sobre velas OHLC. Reciben `[{"time", "open", "high", "low",
"close"}]` (la forma que devuelve `MarketService.candles`) y devuelven
estructuras listas para el frontend. No tocan MT5, la red ni la base: eso los
hace testeables con una lista de dicts y deterministas.

Convención de mitigación
------------------------
Una zona (FVG u OB) queda MITIGADA en cuanto cualquier mecha posterior entra o
cruza la caja, no solo un cierre. Solo se devuelven zonas VIVAS: una zona
mitigada ya no es soporte/resistencia, es memoria.

Portado de `Trinity_proyect/core/smc_engine.py` (D-001), recortado a lo que el
copiloto usa. El módulo es idéntico en comportamiento; cambia el estilo y se
expone `swing_pivots` como público porque `core/levels.py` lo reutiliza.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import List, Optional, Tuple

from core.indicators import calculate_atr

BULLISH_FVG = "BULLISH_FVG"
BEARISH_FVG = "BEARISH_FVG"
BULLISH_OB = "BULLISH_OB"
BEARISH_OB = "BEARISH_OB"
PDH_SWEEP = "PDH_SWEEP"
PDL_SWEEP = "PDL_SWEEP"

_ZONE_LABELS = {
    BULLISH_FVG: "Bullish FVG",
    BEARISH_FVG: "Bearish FVG",
    BULLISH_OB: "Bullish OB",
    BEARISH_OB: "Bearish OB",
}

#: Ventana de confirmación de un swing (velas a cada lado). Es el mismo número
#: que define un BOS: un punto que no sobrevive 5 velas a cada lado no es un
#: swing, es ruido.
BOS_WINDOW = 5


def _r6(x: float) -> float:
    return round(float(x), 6)


def swing_pivots(candles: List[dict], window: int = BOS_WINDOW) -> Tuple[List[int], List[int]]:
    """Índices de swing highs y swing lows con `window` velas a cada lado.

    Un índice `i` es swing high si su `high` es el máximo de la ventana
    `(i-window, i+window)` (excluido `i`); swing low si su `low` es el mínimo.
    Se excluye `i` de la comparación para que una vela plana no sea su propio
    máximo y mínimo.
    """
    highs: List[int] = []
    lows: List[int] = []
    n = len(candles)
    for i in range(window, n - window):
        seg_h = [candles[i + k]["high"] for k in range(-window, window + 1) if i + k != i]
        seg_l = [candles[i + k]["low"] for k in range(-window, window + 1) if i + k != i]
        if candles[i]["high"] >= max(seg_h):
            highs.append(i)
        if candles[i]["low"] <= min(seg_l):
            lows.append(i)
    return highs, lows


def _zone_mitigated(candles: List[dict], from_idx: int, top: float, bottom: float) -> bool:
    """True si alguna mecha a partir de `from_idx` entra o cruza `[bottom, top]`."""
    for c in candles[from_idx:]:
        if c["low"] <= top and c["high"] >= bottom:
            return True
    return False


def detect_fvgs(candles: List[dict]) -> List[dict]:
    """Fair Value Gaps de 3 velas, solo los VIVOS (no mitigados).

    Un FVG bullish deja un hueco cuando la vela `i` abre por encima del high de
    la vela `i-2` (`cur.low > prev.high`); el hueco es `[prev.high, cur.low]`.
    El bearish es el espejo. El tipo distingue `BULLISH_FVG` de `BEARISH_FVG`.
    """
    out: List[dict] = []
    n = len(candles)
    for i in range(2, n):
        prev, cur = candles[i - 2], candles[i]
        if cur["low"] > prev["high"]:
            top, bottom, bullish = cur["low"], prev["high"], True
        elif cur["high"] < prev["low"]:
            top, bottom, bullish = prev["low"], cur["high"], False
        else:
            continue
        if _zone_mitigated(candles, i + 1, top, bottom):
            continue
        out.append({
            "type": BULLISH_FVG if bullish else BEARISH_FVG,
            "top": _r6(top),
            "bottom": _r6(bottom),
            "start_time": cur["time"],
        })
    return out


def _last_opposite_candle(candles: List[dict], before_idx: int, window: int, up: bool) -> Optional[int]:
    """Última vela en dirección contrastaria al impulso, previa al BOS.

    `up=True`  → impulso alcista → la OB es la última vela BAJISTA.
    `up=False` → impulso bajista → la OB es la última vela ALCISTA.
    """
    start = max(0, before_idx - window)
    for j in range(before_idx - 1, start - 1, -1):
        c = candles[j]
        if up and c["close"] < c["open"]:
            return j
        if (not up) and c["close"] > c["open"]:
            return j
    return None


def _append_active_ob(out, candles, ob_idx, bos_idx, ob_type) -> None:
    ob = candles[ob_idx]
    top, bottom = ob["high"], ob["low"]
    # La mitigación se evalúa DESPUÉS de la vela de ruptura del BOS: la propia
    # vela de impulso sale de la zona por definición y no la mitiga.
    if _zone_mitigated(candles, bos_idx + 1, top, bottom):
        return
    out.append({
        "type": ob_type,
        "top": _r6(top),
        "bottom": _r6(bottom),
        "start_time": ob["time"],
    })


def detect_order_blocks(candles: List[dict]) -> List[dict]:
    """Order Blocks: la última vela contraria previa al impulso que rompe estructura.

    Solo los VIVOS: se descartan los que cualquier vela posterior haya vuelto a
    penetrar. Un OB es la base desde la que salió el impulso que rompió un swing,
    y ahí suele quedar liquidez sin ejecutada.
    """
    out: List[dict] = []
    n = len(candles)
    highs, lows = swing_pivots(candles)
    last_sh = None  # (precio, índice) del último swing high confirmado
    last_sl = None  # (precio, índice) del último swing low confirmado
    seen_obs: set = set()  # evita repetir el mismo OB en cada vela de BOS

    hi_p, lo_p = 0, 0
    for i in range(max(BOS_WINDOW, 1), n):
        # Avanzar pivotes confirmados hasta antes de i
        while hi_p < len(highs) and highs[hi_p] < i:
            if last_sh is None or candles[highs[hi_p]]["high"] >= last_sh[0]:
                last_sh = (candles[highs[hi_p]]["high"], highs[hi_p])
            hi_p += 1
        while lo_p < len(lows) and lows[lo_p] < i:
            if last_sl is None or candles[lows[lo_p]]["low"] <= last_sl[0]:
                last_sl = (candles[lows[lo_p]]["low"], lows[lo_p])
            lo_p += 1

        if last_sh and candles[i]["close"] > last_sh[0]:
            ob_idx = _last_opposite_candle(candles, i, BOS_WINDOW, up=True)
            if ob_idx is not None and ob_idx not in seen_obs:
                _append_active_ob(out, candles, ob_idx, i, BULLISH_OB)
                seen_obs.add(ob_idx)
        elif last_sl and candles[i]["close"] < last_sl[0]:
            ob_idx = _last_opposite_candle(candles, i, BOS_WINDOW, up=False)
            if ob_idx is not None and ob_idx not in seen_obs:
                _append_active_ob(out, candles, ob_idx, i, BEARISH_OB)
                seen_obs.add(ob_idx)
    return out


def detect_sweeps(candles: List[dict], pdh: Optional[float], pdl: Optional[float],
                  min_time: Optional[int] = None) -> List[dict]:
    """Liquidity Sweeps: mecha que supera PDH/PDL y cierra de vuelta dentro.

    Es el barrido de liquidez por encima de la resistencia (PDH) o por debajo del
    soporte (PDL): el precio pincha el nivel, atrapa a los que estaban esperando
    el breakout y vuelve. Se reporta el nivel barrido y el extremo de la mecha.
    Si `min_time` se especifica, solo se analizan velas con time >= min_time
    (evitando falsos sweeps de velas anteriores al día del PDH/PDL).
    """
    out: List[dict] = []
    for c in candles:
        if min_time is not None and c.get("time") is not None and c["time"] < min_time:
            continue
        if pdh is not None and c["high"] > pdh and c["close"] < pdh:
            out.append({
                "type": PDH_SWEEP,
                "price_level": _r6(pdh),
                "wick_extreme": _r6(c["high"]),
                "time": c["time"],
            })
        elif pdl is not None and c["low"] < pdl and c["close"] > pdl:
            out.append({
                "type": PDL_SWEEP,
                "price_level": _r6(pdl),
                "wick_extreme": _r6(c["low"]),
                "time": c["time"],
            })
    return out


def analyze(candles: List[dict], pdh: Optional[float], pdl: Optional[float],
            symbol: str = "", timeframe: str = "",
            min_sweep_time: Optional[int] = None) -> dict:
    """El análisis SMC completo: fvgs + order_blocks + sweeps.

    Es la pieza que consume `/api/market/analysis` (y el watcher más adelante).
    """
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "patterns": {
            "fvgs": detect_fvgs(candles),
            "order_blocks": detect_order_blocks(candles),
            "sweeps": detect_sweeps(candles, pdh, pdl, min_time=min_sweep_time),
        },
        "atr_14": calculate_atr(candles, period=14),
    }


def classify_entry(candles: List[dict], pdh: Optional[float], pdl: Optional[float],
                   entry_price: float, entry_time: int) -> dict:
    """Cruza el precio/hora de entrada con las zonas detectadas (para la bitácora).

    Devuelve `{"poi_type": str|None, "liquidity_swept": str|None}`: qué zona SMC
    cubre la entrada y qué liquidez se barrió justo antes.
    """
    j = bisect_right([c["time"] for c in candles], entry_time) - 1
    if j < 0:
        return {"poi_type": None, "liquidity_swept": None}

    parts: List[str] = []

    for fg in detect_fvgs(candles):
        if fg["start_time"] <= candles[j]["time"] and fg["bottom"] <= entry_price <= fg["top"]:
            parts.append(_ZONE_LABELS[fg["type"]])
    for ob in detect_order_blocks(candles):
        if ob["start_time"] <= candles[j]["time"] and ob["bottom"] <= entry_price <= ob["top"]:
            parts.append(_ZONE_LABELS[ob["type"]])

    swept = None
    for sw in detect_sweeps(candles[: j + 1], pdh, pdl):
        idx = _index_of_time(candles, sw["time"])
        if idx is not None and j - idx <= 8:
            swept = "PDH Sweep" if sw["type"] == PDH_SWEEP else "PDL Sweep"
            break

    return {
        "poi_type": " + ".join(dict.fromkeys(parts)) or None,
        "liquidity_swept": swept,
    }


def _index_of_time(candles: List[dict], ts: int) -> Optional[int]:
    for i, c in enumerate(candles):
        if c["time"] == ts:
            return i
    return None


def _analyze_single_htf(
    tf_name: str, candles: List[dict], current_price: Optional[float] = None
) -> Optional[Dict[str, Any]]:
    """Analiza una sola temporalidad alta (`MN1`, `W1`, `D1` o `H4`). PURO.

    Devuelve sesgo estructural (`ALCISTA`/`BAJISTA`/`NEUTRAL`), etiqueta de estado
    (`Alcista`, `Retroceso`, `BOS Bajista`, etc.), posición en el Dealing Range
    (`range_pct` 0-100 y `zone` `PREMIUM`/`DISCOUNT`/`EQUILIBRIUM`), extremos de la
    vela cerrada anterior (`prev_high`, `prev_low`) y zonas SMC activas (`fvgs`, `obs`).
    """
    if not candles:
        return None

    last = candles[-1]
    prev = candles[-2] if len(candles) >= 2 else None
    prev_high = _r6(prev["high"]) if prev is not None else None
    prev_low = _r6(prev["low"]) if prev is not None else None

    price = float(current_price) if current_price is not None else float(last["close"])

    # Dealing Range sobre las últimas hasta 3 velas para medir Premium / Discount real
    ventana = candles[-3:] if len(candles) >= 3 else candles
    r_low = min(float(c["low"]) for c in ventana)
    r_high = max(float(c["high"]) for c in ventana)
    if r_high > r_low:
        raw_pct = (price - r_low) / (r_high - r_low) * 100.0
        range_pct = int(round(max(0.0, min(100.0, raw_pct))))
    else:
        range_pct = 50

    if range_pct >= 55:
        zone = "PREMIUM"
        zone_es = f"Premium {range_pct}%"
    elif range_pct <= 45:
        zone = "DISCOUNT"
        zone_es = f"Discount {range_pct}%"
    else:
        zone = "EQUILIBRIUM"
        zone_es = f"Equilibrio {range_pct}%"

    # Estructura y sesgo de la vela actual respecto a la anterior cerrada
    last_close = float(last["close"])
    bull_candle = last_close >= float(last["open"])
    if prev is not None:
        above_prev_high = last_close > float(prev["high"])
        below_prev_low = last_close < float(prev["low"])
        higher_mid = (float(last["high"]) + float(last["low"])) / 2.0 >= (
            float(prev["high"]) + float(prev["low"])
        ) / 2.0

        if above_prev_high:
            bias = "ALCISTA"
            state_label = "BOS Alcista"
        elif below_prev_low:
            bias = "BAJISTA"
            state_label = "BOS Bajista"
        elif bull_candle and higher_mid:
            bias = "ALCISTA"
            state_label = "Alcista"
        elif (not bull_candle) and (not higher_mid):
            bias = "BAJISTA"
            state_label = "Bajista"
        elif higher_mid and not bull_candle:
            bias = "BAJISTA"
            state_label = "Retroceso"
        elif (not higher_mid) and bull_candle:
            bias = "ALCISTA"
            state_label = "Rebote"
        else:
            bias = "ALCISTA" if bull_candle else "BAJISTA"
            state_label = "Alcista" if bull_candle else "Bajista"
    else:
        bias = "ALCISTA" if bull_candle else "BAJISTA"
        state_label = "Alcista" if bull_candle else "Bajista"

    fvgs = detect_fvgs(candles)
    obs = detect_order_blocks(candles)

    # Identificar si el precio está dentro o muy cerca de un POI de esta temporalidad
    active_poi = None
    nearest_zone = None
    best_dist = float("inf")

    for z in obs + fvgs:
        top = float(z["top"])
        btm = float(z["bottom"])
        z_short = (
            "OB+" if z["type"] == BULLISH_OB
            else "OB-" if z["type"] == BEARISH_OB
            else "FVG+" if z["type"] == BULLISH_FVG
            else "FVG-"
        )
        if btm <= price <= top:
            dist = 0.0
            active_poi = f"{z_short} Activo"
        else:
            dist = min(abs(price - top), abs(price - btm))

        if dist < best_dist:
            best_dist = dist
            nearest_zone = {
                "timeframe": tf_name,
                "type": z["type"],
                "short_label": z_short,
                "label": f"{tf_name} {_ZONE_LABELS.get(z['type'], z['type']).upper()}",
                "bottom": _r6(btm),
                "top": _r6(top),
                "start_time": z["start_time"],
                "inside": btm <= price <= top,
            }

    if active_poi is None and nearest_zone is not None and (r_high > r_low):
        # Si está a menos del 15% del rango respecto a la zona, se destaca como activa/cercana
        if best_dist <= (r_high - r_low) * 0.15:
            active_poi = f"{nearest_zone['short_label']} Activo"

    return {
        "timeframe": tf_name,
        "bias": bias,
        "state_label": state_label,
        "range_pct": range_pct,
        "zone": zone,
        "zone_label": zone_es,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "active_poi": active_poi,
        "nearest_zone": nearest_zone,
        "fvgs_count": len(fvgs),
        "obs_count": len(obs),
    }


def analyze_htf_context(
    htf_candles: Optional[Dict[str, List[dict]]],
    current_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Construye el contexto multi-temporal (`MN1`, `W1`, `D1`, `H4`) y niveles mayores.

    Recibe un diccionario `{"MN1": [...], "W1": [...], "D1": [...], "H4": [...]}`
    (cualquiera puede faltar y se omite sin error) y devuelve los niveles de liquidez
    de periodo previo (`pdh`/`pdl`, `pwh`/`pwl`, `pmh`/`pml`), la matriz por
    temporalidad y la zona institucional mayor más cercana (`htf_zone`).
    """
    bundle = htf_candles or {}
    tf_map: Dict[str, Dict[str, Any]] = {}
    for tf_name in ("MN1", "W1", "D1", "H4"):
        velas = bundle.get(tf_name) or []
        info = _analyze_single_htf(tf_name, velas, current_price=current_price)
        if info is not None:
            tf_map[tf_name] = info

    d1 = tf_map.get("D1") or {}
    w1 = tf_map.get("W1") or {}
    mn1 = tf_map.get("MN1") or {}

    # Elegir la zona HTF más relevante (priorizando W1 y D1) para dibujar en el gráfico
    htf_zone = None
    for candidate_tf in ("W1", "D1", "H4"):
        z = (tf_map.get(candidate_tf) or {}).get("nearest_zone")
        if z is not None:
            htf_zone = z
            if z.get("inside"):
                break

    return {
        "pdh": d1.get("prev_high"),
        "pdl": d1.get("prev_low"),
        "pwh": w1.get("prev_high"),
        "pwl": w1.get("prev_low"),
        "pmh": mn1.get("prev_high"),
        "pml": mn1.get("prev_low"),
        "timeframes": tf_map,
        "htf_zone": htf_zone,
    }


def evaluate_htf_confluence(
    htf_context: Optional[Dict[str, Any]],
    direction: Optional[str] = None,
    current_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Evalúa cómo el contexto HTF (`MN1`, `W1`, `D1`, `H4`) apoya o frena la dirección M15.

    Reglas SMC aplicadas:
    - Vender en zona Premium (`D1`/`W1` >= 55%) o comprar en zona Discount (<= 45%)
      otorga bonificación de ubicación (+5 pts).
    - Alineación de estructura con `D1` (+4 pts), `W1` (+4 pts) y `H4` (+2 pts).
    - Presencia de un POI mayor (`OB` o `FVG` en `W1`/`D1`) a favor (+3 pts).
    - Identifica el próximo imán de liquidez mayor (`PWL`, `PDL`, `PML` en SELL;
      `PWH`, `PDH`, `PMH` en BUY).
    """
    ctx = htf_context or {}
    tf_map = ctx.get("timeframes") or {}
    dir_up = str(direction or "").strip().upper()
    is_buy = dir_up == "BUY"
    is_sell = dir_up == "SELL"
    expected_bias = "ALCISTA" if is_buy else ("BAJISTA" if is_sell else None)

    bonus = 0.0
    reasons: List[str] = []
    warning: Optional[str] = None

    d1 = tf_map.get("D1")
    w1 = tf_map.get("W1")
    h4 = tf_map.get("H4")

    if expected_bias:
        # 1. Ubicación Premium / Discount en D1 (o W1 si no hay D1)
        ref_tf = d1 or w1
        if ref_tf:
            tf_lbl = ref_tf["timeframe"]
            pct = ref_tf["range_pct"]
            if is_sell and ref_tf["zone"] == "PREMIUM":
                bonus += 5.0
                reasons.append(f"Venta en zona Premium {tf_lbl} ({pct}%)")
            elif is_buy and ref_tf["zone"] == "DISCOUNT":
                bonus += 5.0
                reasons.append(f"Compra en zona Discount {tf_lbl} ({pct}%)")
            elif is_buy and pct >= 78:
                bonus -= 5.0
                warning = f"Comprando en Premium extremo {tf_lbl} ({pct}%)"
            elif is_sell and pct <= 22:
                bonus -= 5.0
                warning = f"Vendiendo en Discount extremo {tf_lbl} ({pct}%)"

        # 2. Alineación de sesgo estructural D1 / W1 / H4
        opposite_bias = "BAJISTA" if is_buy else "ALCISTA"
        aligned_tfs: List[str] = []
        against_tfs: List[str] = []
        if d1 and d1["bias"] == expected_bias:
            bonus += 4.0
            aligned_tfs.append("D1")
        elif d1 and d1["bias"] == opposite_bias:
            bonus -= 3.0
            against_tfs.append("D1")
        if w1 and w1["bias"] == expected_bias:
            bonus += 4.0
            aligned_tfs.append("W1")
        elif w1 and w1["bias"] == opposite_bias:
            bonus -= 3.0
            against_tfs.append("W1")
        if h4 and h4["bias"] == expected_bias:
            bonus += 2.0
            aligned_tfs.append("H4")
        elif h4 and h4["bias"] == opposite_bias:
            bonus -= 1.5
            against_tfs.append("H4")
        if aligned_tfs:
            reasons.append(f"Alineado con {'/'.join(aligned_tfs)}")
        if against_tfs and warning is None:
            warning = f"Contra tendencia {'/'.join(against_tfs)}"

        # 3. Confluencia con zona institucional mayor (OB/FVG en W1 o D1)
        for htf_item in (w1, d1):
            if not htf_item:
                continue
            poi = htf_item.get("active_poi") or ""
            if (is_buy and "+" in poi) or (is_sell and "-" in poi):
                bonus += 3.0
                reasons.append(f"{poi} en {htf_item['timeframe']}")
                break
            if (is_buy and "-" in poi) or (is_sell and "+" in poi):
                if warning is None:
                    warning = f"Frente a {poi} opuesto en {htf_item['timeframe']}"

    bonus = round(max(-10.0, min(15.0, bonus)), 1)

    # 4. Próximo objetivo de liquidez mayor (PDH/PDL, PWH/PWL, PMH/PML)
    next_liquidity = None
    if current_price is not None and (is_buy or is_sell):
        px = float(current_price)
        candidates = []
        if is_buy:
            for lbl, val, scope in (
                ("PDH", ctx.get("pdh"), "Diario"),
                ("PWH", ctx.get("pwh"), "Semanal"),
                ("PMH", ctx.get("pmh"), "Mensual"),
            ):
                if val is not None and float(val) > px:
                    candidates.append((float(val) - px, lbl, float(val), scope))
        else:
            for lbl, val, scope in (
                ("PDL", ctx.get("pdl"), "Diario"),
                ("PWL", ctx.get("pwl"), "Semanal"),
                ("PML", ctx.get("pml"), "Mensual"),
            ):
                if val is not None and float(val) < px:
                    candidates.append((px - float(val), lbl, float(val), scope))

        if candidates:
            # Si hay PWH/PWL disponible lo priorizamos como objetivo estructural, o el más cercano
            candidates.sort(key=lambda item: item[0])
            _, lbl, target_px, scope = candidates[0]
            next_liquidity = {
                "label": lbl,
                "price": _r6(target_px),
                "scope": scope,
            }

    if reasons:
        sign = "+" if bonus >= 0 else ""
        summary = f"{sign}{int(round(bonus))} pts ({reasons[0]})"
    elif warning:
        summary = f"{int(round(bonus))} pts ({warning})"
    else:
        summary = "Neutral (sin sesgo dominante)"

    return {
        "score_bonus": bonus,
        "summary": summary,
        "reasons": reasons,
        "warning": warning,
        "next_liquidity": next_liquidity,
        "timeframes": tf_map,
        "htf_zone": ctx.get("htf_zone"),
        "levels": {
            "pdh": ctx.get("pdh"),
            "pdl": ctx.get("pdl"),
            "pwh": ctx.get("pwh"),
            "pwl": ctx.get("pwl"),
            "pmh": ctx.get("pmh"),
            "pml": ctx.get("pml"),
        },
    }


__all__ = [
    "BOS_WINDOW",
    "BEARISH_FVG",
    "BEARISH_OB",
    "BULLISH_FVG",
    "BULLISH_OB",
    "PDH_SWEEP",
    "PDL_SWEEP",
    "analyze",
    "analyze_htf_context",
    "classify_entry",
    "detect_fvgs",
    "detect_order_blocks",
    "detect_sweeps",
    "evaluate_htf_confluence",
    "swing_pivots",
]
