"""Política de salida en dos tramos (gestión parcial escalada).

Cierra el primer tramo (`partial_volume_pct` %) al alcanzar `partial_ratio_r`
múltiplos de R, mueve el SL del resto a break-even + offset (anti-spread) y
deja correr el resto hacia `runner_ratio_r`.

Por qué vive en `core/` y no en el servicio: TODO el dinero que mueve una
política de salida tiene que ser comprobable sin bróker, sin base y sin FastAPI.
El servicio (o el watcher, en el futuro) decide CUÁNDO llamar aquí; esta capa
decide QUÉ se calcularía, con números exactos y sin I/O. La pureza la vigila
`tests/unit/test_core_purity.py`, y los redondeos de lote los prueba
`tests/unit/test_exit_policy.py`.

Los tres conceptos que sostienen la política:

- **R original.** El múltiplo de R se mide contra la distancia SL→entrada de
  la APERTURA, no contra el SL actual. En cuanto el SL sube a break-even, la
  distancia SL→entrada es ~0 y "1:5" dejaría de significar nada: por eso el R
  original se calcula una vez (de la posición recién abierta) y se guarda en
  `position_state` para todo lo demás.
- **Ratchet ("nunca afloja").** El break-even solo se aplica si mejora el stop
  actual. Un stop movido a mano más cerca del precio, o un trailing previo, no
  se reemplaza por un BE peor: `ratchet_sl` queda con el mejor de los dos.
- **Offset de break-even.** El BE no se coloca en el precio de entrada
  milimétrico: un spread flotante (especialmente en XAUUSD) sacaría la
  posición en "cero" con una pérdida real de varios dólares por lote. El
  offset, en unidades de precio igual que `sl_distance`, empuja el stop un
  colchón del lado del beneficio.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from core.strategy import DEFAULTS

#: Tolerancia float para comparar precios y volúmenes (redondeos de rejilla).
EPS = 1e-9


def partial_ratio_for(cfg: Optional[Dict[str, Any]]) -> float:
    """El R del primer tramo (asegurar caja), con el default del sistema."""
    return _ratio(cfg, "partial_ratio_r", float(DEFAULTS["partial_ratio_r"]))


def runner_ratio_for(cfg: Optional[Dict[str, Any]]) -> float:
    """El R del tramo restante (dejar correr), con el default del sistema."""
    return _ratio(cfg, "runner_ratio_r", float(DEFAULTS["runner_ratio_r"]))


def partial_volume_pct_for(cfg: Optional[Dict[str, Any]]) -> float:
    """El % de la posición que se cierra en el primer tramo (0-100)."""
    raw = _get(cfg, "partial_volume_pct")
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULTS["partial_volume_pct"])
    if pct <= 0 or pct > 100:
        return float(DEFAULTS["partial_volume_pct"])
    return pct


def breakeven_offset_for(cfg: Optional[Dict[str, Any]],
                         symbol: Optional[str] = None) -> float:
    """Offset del break-even en UNIDADES DE PRECIO, por símbolo o global.

    `breakeven_offset_by_symbol` manda sobre el global, igual que
    `sl_distance_by_symbol` sobre `sl_distance`: un único número no puede ser
    el colchón correcto para EURUSD (0.00005) y para el oro (0.30) a la vez.
    """
    if symbol:
        by_symbol = _get(cfg, "breakeven_offset_by_symbol")
        if isinstance(by_symbol, dict):
            try:
                off = float(by_symbol.get(str(symbol).strip().upper()))
            except (TypeError, ValueError):
                off = None
            if off is not None and off >= 0:
                return off
    raw = _get(cfg, "breakeven_offset")
    try:
        off = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULTS["breakeven_offset"])
    return off if off >= 0 else float(DEFAULTS["breakeven_offset"])


def r_original_from(sl: Optional[float], price_open: Optional[float]
                    ) -> Optional[float]:
    """La distancia original SL→entrada (un R), o `None` si no se puede medir.

    Un SL a 0 (sin stop) no es una distancia: es la ausencia de stop, y de ahí
    no sale un R. Un SL ya movido a break-even da ~0, que tampoco es un R
    utilizable; en ese caso el caller debe usar el `r_original` guardado en
    `position_state`.
    """
    try:
        sl_f = float(sl or 0.0)
        open_f = float(price_open or 0.0)
    except (TypeError, ValueError):
        return None
    if sl_f <= 0 or open_f <= 0:
        return None
    distancia = abs(open_f - sl_f)
    return distancia if distancia > EPS else None


def r_actual(direction: str, price_open: Optional[float],
             price_current: Optional[float], r_original: Optional[float]
             ) -> Optional[float]:
    """Múltiplos de R que lleva la posición AHORA, o `None` sin base de medición.

    BUY gana cuando el precio sube, SELL cuando baja; el signo se aplica aquí
    para que el llamador no tenga que recordarlo en cada sitio.
    """
    if not r_original or r_original <= EPS:
        return None
    try:
        open_f = float(price_open or 0.0)
        actual = float(price_current or 0.0)
    except (TypeError, ValueError):
        return None
    if open_f <= 0 or actual <= 0:
        return None
    signo = 1.0 if str(direction).upper() == "BUY" else -1.0
    return signo * (actual - open_f) / float(r_original)


def partial_close_volume(volume: Optional[float], pct: float,
                         volume_min: float, volume_step: float
                         ) -> Dict[str, Any]:
    """`(dict)` con el volumen del primer tramo, o el motivo de no haberlo.

    El volumen baja SIEMPRE con floor al `volume_step` (nunca se cierra más de
    lo pedido) y se rechaza si quedaría por debajo del `volume_min` del bróker
    o si el resto no alcanzaría el mínimo: cerrar 0.005 de 0.01 no es un
    parcial, es un rechazo con forma de orden.
    """
    try:
        vol = float(volume or 0.0)
    except (TypeError, ValueError):
        vol = 0.0
    if vol <= 0:
        return {"volume": None, "reason": "La posición no tiene volumen."}
    try:
        pct_f = float(pct)
    except (TypeError, ValueError):
        pct_f = float(DEFAULTS["partial_volume_pct"])
    if pct_f <= 0 or pct_f > 100:
        pct_f = float(DEFAULTS["partial_volume_pct"])

    vmin = float(volume_min or 0.01)
    vstep = float(volume_step or 0.01) or 0.01

    bruto = vol * pct_f / 100.0
    cerrar = _step_floor(bruto, vstep)
    if cerrar < vmin:
        return {"volume": None, "reason": (
            "El primer tramo ({0:g} lotes) quedaría por debajo del mínimo del "
            "bróker ({1:g}): no se parte la posición; aplica solo break-even."
            .format(cerrar, vmin))}
    restante = _step_floor(vol - cerrar, vstep)
    if restante < vmin:
        return {"volume": None, "reason": (
            "El resto ({0:g} lotes) quedaría por debajo del mínimo del bróker "
            "({1:g}): no se parte la posición; aplica solo break-even."
            .format(restante, vmin))}
    return {"volume": round(cerrar, 10), "remaining": round(restante, 10),
            "reason": None}


def breakeven_sl(direction: str, price_open: Optional[float],
                  offset: float) -> Optional[float]:
    """El nivel de break-even + offset: `None` si no hay entrada utilizable.

    BUY: entrada + offset (el stop sube por debajo del precio actual). SELL:
    entrada − offset. El offset es el colchón contra el spread flotante.
    """
    try:
        open_f = float(price_open or 0.0)
    except (TypeError, ValueError):
        return None
    if open_f <= 0:
        return None
    try:
        off = max(0.0, float(offset or 0.0))
    except (TypeError, ValueError):
        off = 0.0
    signo = 1.0 if str(direction).upper() == "BUY" else -1.0
    return open_f + signo * off


def ratchet_sl(direction: str, current_sl: Optional[float],
                proposed_sl: Optional[float]) -> Optional[float]:
    """El stop que NO afloja: queda con el mejor de los dos, o el propuesto.

    Para BUY, stop más alto = más cerca del precio = mejor (menor riesgo).
    Para SELL, stop más bajo = mejor. Un stop actual a 0 (sin nivel) acepta el
    propuesto tal cual. `proposed_sl` a `None` es "no proponer nada": se
    conserva el actual.
    """
    if proposed_sl is None:
        return current_sl
    try:
        prop = float(proposed_sl)
    except (TypeError, ValueError):
        return current_sl
    try:
        cur = float(current_sl or 0.0)
    except (TypeError, ValueError):
        cur = 0.0
    if cur <= 0:
        return prop
    if str(direction).upper() == "BUY":
        return max(cur, prop)
    return min(cur, prop)


def runner_level(direction: str, price_open: Optional[float],
                  r_original: Optional[float], runner_ratio: float
                  ) -> Optional[float]:
    """El nivel objetivo del tramo restante: `None` sin base de medición."""
    if not r_original or r_original <= EPS:
        return None
    try:
        open_f = float(price_open or 0.0)
    except (TypeError, ValueError):
        return None
    if open_f <= 0:
        return None
    try:
        ratio = float(runner_ratio)
    except (TypeError, ValueError):
        ratio = float(DEFAULTS["runner_ratio_r"])
    if ratio <= 0:
        return None
    signo = 1.0 if str(direction).upper() == "BUY" else -1.0
    return open_f + signo * float(r_original) * ratio


def management_view(position: Dict[str, Any], state: Optional[Dict[str, Any]],
                    cfg: Optional[Dict[str, Any]], volume_min: float,
                    volume_step: float) -> Dict[str, Any]:
    """La vista de gestión de UNA posición: qué se puede aplicar y qué falta.

    Entrada normalizada del puerto (`_fila`) más el estado persistido
    (`position_state`) y la config plana. Devuelve todo lo que el panel y el
    servicio necesitan para decidir y para enseñar el plan al operador, sin
    tocar nada: es una consulta, no una orden.

    El `r_original` sale del estado si existe (la única fuente fiable una vez
    movido el SL a BE); si no, se infiere del SL actual de la posición, que es
    el caso normal de la primera vez. Si tampoco eso es medible (SL ya en BE
    sin estado), `r_original` queda `None` y el caller puede forzarlo.
    """
    pos = position or {}
    state = state or {}
    direction = str(pos.get("type") or pos.get("direction") or "").upper()
    price_open = _float_or_none(pos.get("price_open"))
    price_current = _float_or_none(pos.get("price_current"))
    volume = _float_or_none(pos.get("volume")) or 0.0
    sl_actual = _float_or_none(pos.get("sl"))

    r_orig = _float_or_none(state.get("r_original"))
    if not r_orig or r_orig <= EPS:
        r_orig = r_original_from(sl_actual, price_open)

    r_act = r_actual(direction, price_open, price_current, r_orig)
    p_ratio = partial_ratio_for(cfg)
    run_ratio = runner_ratio_for(cfg)
    offset = breakeven_offset_for(cfg, pos.get("symbol"))

    parcial = partial_close_volume(volume, partial_volume_pct_for(cfg),
                                   volume_min, volume_step)
    be_level = breakeven_sl(direction, price_open, offset)
    run_level = runner_level(direction, price_open, r_orig, run_ratio)

    partial_taken = bool(int(state.get("partial_taken") or 0))
    be_applied = bool(int(state.get("be_applied") or 0))
    set_runner_tp = _get(cfg, "set_runner_tp")
    if not isinstance(set_runner_tp, bool):
        set_runner_tp = bool(DEFAULTS["set_runner_tp"])

    partial_ready = r_act is not None and r_act >= p_ratio - EPS
    runner_ready = r_act is not None and r_act >= run_ratio - EPS

    motivos: list[str] = []
    if direction not in ("BUY", "SELL"):
        motivos.append("Dirección desconocida en la posición.")
    if not r_orig or r_orig <= EPS:
        motivos.append("No hay R original medible (SL ausente o ya en "
                       "break-even sin estado guardado): pásalo a mano.")
    if partial_taken:
        motivos.append("El primer tramo ya se tomó: queda solo el runner.")
    elif not partial_ready:
        motivos.append("El precio aún no alcanzó {0:g}R.".format(p_ratio))

    can_apply = (direction in ("BUY", "SELL")
                 and bool(r_orig and r_orig > EPS)
                 and (partial_ready or partial_taken))

    return {
        "ticket": pos.get("ticket"),
        "symbol": pos.get("symbol"),
        "direction": direction,
        "volume": volume,
        "price_open": price_open,
        "price_current": price_current,
        "r_original": r_orig,
        "r_actual": r_act,
        "partial_ratio_r": p_ratio,
        "runner_ratio_r": run_ratio,
        "partial_ready": partial_ready,
        "partial_taken": partial_taken,
        "be_applied": be_applied,
        "partial_volume": parcial.get("volume"),
        "partial_reason": parcial.get("reason"),
        "be_level": be_level,
        "runner_level": run_level,
        "runner_ready": runner_ready,
        "set_runner_tp": set_runner_tp,
        "can_apply": can_apply,
        "reasons": motivos,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get(cfg: Optional[Dict[str, Any]], key: str) -> Any:
    """Clave plana del config, aceptando también la forma anidada del YAML."""
    if not isinstance(cfg, dict):
        return None
    if key in cfg:
        return cfg[key]
    seccion = cfg.get("execution")
    if isinstance(seccion, dict):
        return seccion.get(key)
    return None


def _ratio(cfg: Optional[Dict[str, Any]], key: str, default: float) -> float:
    raw = _get(cfg, key)
    if isinstance(raw, bool):
        return default
    try:
        valor = float(raw)
    except (TypeError, ValueError):
        return default
    return valor if valor > 0 else default


def _float_or_none(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _step_floor(valor: float, paso: float) -> float:
    """Floor del `valor` a la rejilla `paso` (nunca se expone más de lo pedido)."""
    if not paso or paso <= 0:
        return round(valor, 10)
    return round(math.floor(valor / paso + EPS) * paso, 10)


__all__ = [
    "breakeven_offset_for",
    "breakeven_sl",
    "management_view",
    "partial_close_volume",
    "partial_ratio_for",
    "partial_volume_pct_for",
    "r_actual",
    "r_original_from",
    "ratchet_sl",
    "runner_level",
    "runner_ratio_for",
]
