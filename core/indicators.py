"""Indicadores técnicos y microestructura PURA.

Módulo determinista: sin I/O, sin MT5, sin bases de datos.
Testeable 100% de forma aislada bajo `tests/unit/test_core_purity.py`.
"""

from __future__ import annotations

import math
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
    atr_min_mult: float = 1.0,
) -> Dict[str, Any]:
    """SL anclado a la estructura, con volatilidad como piso real, y TP al imán.

    La distancia del stop es la MAYOR de tres candidatos, y se publica cuál ganó
    (`dist_source`) porque son tres tesis distintas y confundirlas fue el bug de
    M21 (un stop de 12 pips fijos se anunciaba como "estructural"):

      - `structure`: |entrada - (distal ± buffer)|, con
        `buffer = max(0.15*ATR, 2*spread, 1 tick)`. Es la estructura del gráfico.
      - `atr`: `atr_min_mult * ATR`. Un stop DENTRO de una ATR de volatilidad es
        ruido, no un stop: si la zona es más estrecha que la volatilidad, manda
        la volatilidad.
      - `config`: `sl_min`, la distancia mínima que pide el operador.

    `is_structural` es honesto: solo es `True` cuando gana `structure`. Antes se
    publicaba `True` con solo existir un distal, aunque `sl_min` fijara el stop.

    El redondeo se hace UNA vez, sobre la distancia, y de ahí se derivan SL y TP.
    Redondear los dos niveles por separado desde una distancia sin redondear
    dejaba R:R en 1.99 y `validate_entry` rechazaba el setup (13 de 20.000). El
    TP además se redondea HACIA ARRIBA al tick para que el R:R nunca caiga por
    debajo del ratio pedido.
    """
    e = float(entry)
    dir_clean = str(direction or "BUY").strip().upper()
    is_buy = dir_clean != "SELL"
    ratio = float(tp_ratio) if tp_ratio and tp_ratio > 0 else 2.0
    d = max(0, min(8, int(digits)))
    paso = 10.0 ** (-d)

    atr_val = float(atr or 0.0)
    sp_val = float(spread or 0.0)
    tick_val = float(tick_size or 0.00001)
    min_dist = max(0.0, float(sl_min or 0.0))
    atr_floor = max(0.0, float(atr_min_mult or 0.0)) * atr_val

    # Colchón de volatilidad institucional alrededor de la zona distal.
    buffer = max(0.15 * atr_val, 2.0 * sp_val, 1.0 * tick_val)

    # signo del STOP respecto a la entrada: BUY lo pone debajo, SELL encima.
    signo_sl = -1.0 if is_buy else 1.0
    signo_tp = 1.0 if is_buy else -1.0

    distal_val = float(distal) if distal is not None and distal > 0 else None

    if distal_val is not None:
        stop_estructural = distal_val - buffer if is_buy else distal_val + buffer
        # Distancia POSITIVA hasta ese stop; un distal del lado equivocado da
        # negativo y entonces no aporta nada (no se inventa una distancia).
        structural_dist = max(0.0, signo_sl * (stop_estructural - e))
        candidatos = [
            ("structure", structural_dist),
            ("atr", atr_floor),
            ("config", min_dist),
        ]
    else:
        # Sin zona identificada: 1.5 ATR de volatilidad, nunca menos que el piso.
        vol_dist = 1.5 * atr_val if atr_val > 0 else 0.0
        candidatos = [
            ("atr", max(vol_dist, atr_floor)),
            ("config", min_dist),
        ]

    # `max` devuelve el PRIMER máximo: los empates los gana el candidato más
    # específico (structure > atr > config), que es el orden de la lista.
    dist_source, final_dist = max(candidatos, key=lambda par: par[1])
    if final_dist <= 0:
        # Seguro absoluto: una distancia 0 sería un stop en la entrada.
        dist_source, final_dist = "fallback", 10.0 * tick_val

    # Un solo redondeo, sobre la distancia.
    dist_r = round(final_dist, d) or paso
    sl = round(e + signo_sl * dist_r, d)

    # TP: techo al tick para que el R:R no caiga por debajo de `ratio`.
    tp_dist_r = round(math.ceil(dist_r * ratio / paso - 1e-9) * paso, d)
    tp_dist_r = max(tp_dist_r, dist_r * ratio)
    magnet_applied = False

    if target_magnet is not None and float(target_magnet or 0.0) > 0:
        mag = float(target_magnet)
        mag_dist = signo_tp * (mag - e)
        # El imán solo vale si respeta el R:R que se exige al TP; si está más
        # cerca, se usa el TP por ratio (el imán no recorta el objetivo).
        if mag_dist > 0 and mag_dist + paso >= dist_r * max(2.0, ratio):
            tp = round(mag, d)
            tp_dist_r = round(abs(tp - e), d)
            magnet_applied = True
        else:
            tp = round(e + signo_tp * tp_dist_r, d)
    else:
        tp = round(e + signo_tp * tp_dist_r, d)

    return {
        "entry": round(e, d),
        "sl": sl,
        "tp": tp,
        # Las distancias publicadas son las REALES de los niveles redondeados.
        "sl_distance": round(abs(e - sl), d),
        "tp_distance": round(abs(tp - e), d),
        "buffer": round(buffer, d),
        "is_structural": dist_source == "structure",
        "dist_source": dist_source,
        "atr_floor": round(atr_floor, d),
        "magnet_applied": magnet_applied,
        "atr_used": round(atr_val, d),
    }

