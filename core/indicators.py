"""Indicadores técnicos y microestructura PURA.

Módulo determinista: sin I/O, sin MT5, sin bases de datos.
Testeable 100% de forma aislada bajo `tests/unit/test_core_purity.py`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def calculate_atr(candles: Sequence[Dict[str, Any]], period: int = 14) -> float:
    """Calcula el Average True Range (ATR) de Wilder sobre una serie de velas.
    
    Cada vela debe tener 'high', 'low', 'close'.
    Si hay menos de period + 1 velas, calcula un promedio sobre las disponibles
    o retorna 0.0 si la serie está vacía.
    """
    if not candles:
        return 0.0

    p = max(1, int(period))
    n = len(candles)
    if n == 1:
        c0 = candles[0]
        return max(0.0, float(c0.get("high", 0.0)) - float(c0.get("low", 0.0)))

    # 1. Calcular True Range (TR)
    trs: List[float] = []
    # Primera barra: High - Low
    c0 = candles[0]
    trs.append(max(0.0, float(c0.get("high", 0.0)) - float(c0.get("low", 0.0))))

    for i in range(1, n):
        curr = candles[i]
        prev = candles[i - 1]
        h = float(curr.get("high", 0.0))
        l = float(curr.get("low", 0.0))
        prev_c = float(prev.get("close", 0.0))

        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)

    # Si hay menos velas que el periodo, retornamos la media simple de los TRs
    if len(trs) < p:
        return round(sum(trs) / len(trs), 6)

    # 2. Inicialización: SMA de los primeros `p` TRs
    atr_val = sum(trs[:p]) / p

    # 3. Suavizado exponencial de Wilder
    for tr in trs[p:]:
        atr_val = ((atr_val * (p - 1)) + tr) / p

    return round(float(atr_val), 6)


def structural_sl_and_tp(
    entry: float,
    direction: str,
    distal: Optional[float] = None,
    atr: Optional[float] = None,
    spread: float = 0.0,
    tick_size: float = 0.00001,
    sl_min: float = 0.0,
    tp_ratio: float = 2.0,
    target_magnet: Optional[float] = None,
    digits: int = 5,
) -> Dict[str, Any]:
    """Calcula SL estructural anclado al distal de la zona + colchón ATR(14) y TP al imán.

    Fórmula:
      buffer = max(0.15 * atr, 2.0 * spread, 1.0 * tick_size)
      sl_estructural = distal - buffer (BUY) ó distal + buffer (SELL)
      sl_distancia = max(sl_distancia_estructural, sl_min)
      lote se dimensiona sobre esa distancia real.
    """
    e = float(entry)
    dir_clean = str(direction or "BUY").strip().upper()
    is_buy = dir_clean != "SELL"
    ratio = float(tp_ratio) if tp_ratio and tp_ratio > 0 else 2.0
    d = max(0, min(8, int(digits)))

    atr_val = float(atr or 0.0)
    sp_val = float(spread or 0.0)
    tick_val = float(tick_size or 0.00001)
    min_dist = max(0.0, float(sl_min or 0.0))

    # Colchón de volatilidad institucional
    buffer = max(0.15 * atr_val, 2.0 * sp_val, 1.0 * tick_val)

    is_structural = False
    distal_val = float(distal) if distal is not None and distal > 0 else None

    if distal_val is not None:
        is_structural = True
        if is_buy:
            sl_raw = distal_val - buffer
            structural_dist = e - sl_raw
            final_dist = max(structural_dist, min_dist)
            sl = e - final_dist
        else:
            sl_raw = distal_val + buffer
            structural_dist = sl_raw - e
            final_dist = max(structural_dist, min_dist)
            sl = e + final_dist
    else:
        # Fallback sin zona identificada: volatilidad ATR 1.5x o mínimo
        vol_dist = 1.5 * atr_val if atr_val > 0 else min_dist
        final_dist = max(vol_dist, min_dist)
        if final_dist <= 0:
            final_dist = 10 * tick_val  # Seguro absoluto
        sl = e - final_dist if is_buy else e + final_dist

    # Take Profit (imán de liquidez si cumple >= 2R, o ratio R directo)
    tp_dist = final_dist * ratio
    magnet_applied = False

    if target_magnet is not None and target_magnet > 0:
        mag = float(target_magnet)
        if is_buy and mag > e:
            mag_dist = mag - e
            if mag_dist >= final_dist * 2.0:
                tp = mag
                tp_dist = mag_dist
                magnet_applied = True
            else:
                tp = e + tp_dist
        elif not is_buy and mag < e:
            mag_dist = e - mag
            if mag_dist >= final_dist * 2.0:
                tp = mag
                tp_dist = mag_dist
                magnet_applied = True
            else:
                tp = e - tp_dist
        else:
            tp = e + tp_dist if is_buy else e - tp_dist
    else:
        tp = e + tp_dist if is_buy else e - tp_dist

    return {
        "entry": round(e, d),
        "sl": round(sl, d),
        "tp": round(tp, d),
        "sl_distance": round(final_dist, d),
        "tp_distance": round(tp_dist, d),
        "buffer": round(buffer, d),
        "is_structural": is_structural,
        "magnet_applied": magnet_applied,
        "atr_used": round(atr_val, d),
    }
