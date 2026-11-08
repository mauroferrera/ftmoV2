"""La puerta del setup: zona de entrada, niveles y veredicto de aprobación.

Modulo PURO, como el resto de `core/`: no lee ficheros, no habla con el bróker y no
importa `adapters`. Recibe el snapshot ya armado y la config ya aplanada, y devuelve
el plan (dirección, entry, SL, TP) con la razón exacta de cada rechazo.

Por qué vive aquí y no en el servicio del watcher
-------------------------------------------------
En REF esta lógica estaba en `watcher.py`, que además escaneaba símbolos en bucle,
guardaba deduplicaciones y mandaba órdenes. Al separar el cálculo del ciclo, la mitad
que decide **si un setup es operable** queda testeable sin terminal, sin base de datos
y sin reloj real.

Lo que este módulo NO hace
--------------------------
- No avisa de calidad de ejecución. REF tenía aquí un bloque
  (`execution_quality.evaluate`) que medía spread y distancia a la invalidez como
  AVISO, sin rechazar. Ese módulo no está portado, así que aquí no hay ni el bloque ni
  una lista de avisos vacía que sugiera que se midió algo: una clave `warnings`
  siempre vacía es indistinguible de "se midió y no hay nada que decir".
- No conoce el `SymbolSpec`. Cuando hace falta, lo usa por `getattr` (lo mismo que
  `core.strategy.sl_distance_for`): la dependencia va de `adapters` hacia `core`, y el
  núcleo no puede importar al adaptador ni para leer unos dígitos.
- No sabe de noticias ni de límites de cuenta: eso es de la ejecución (M5).

La killzone es un FACTOR de score, no un AND duro (D-007)
---------------------------------------------------------
A diferencia de REF, aquí `approved` NO exige `in_killzone`. La ventana ya puntúa:
`core.risk_engine.DEFAULT_WEIGHTS["killzone"]` vale dentro y fuera (1.0 / 0.25), así
que exigirla además como AND contaría la misma variable dos veces. El gate publica
`in_killzone` y `killzone_name` como INFORMACIÓN (para que la UI diga si el setup cayó
en sesión), pero el veredicto es score + zona alcanzable + SL + `validate_entry`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import indicators, risk_engine, smc_engine, strategy

#: R:R por defecto cuando la config no declara `tp_ratio_r`. Es el 2.0 de REF: con un
#: valor accidentalmente nulo el TP caería en la propia entrada y el "plan" sería una
#: orden con objetivo en el sitio de donde ya está el precio.
DEFAULT_TP_RATIO = 2.0

#: Múltiplo de la distancia de SL que se admite entre el precio y la zona de entrada.
#: Acotado a 0.5 para coherencia estricta con max_slippage_sl_share de execution_quality.
ENTRY_ZONE_MAX_SPAN_SL_MULT = 0.5

#: Fallback ABSOLUTO en precio, solo cuando no se conoce ninguna distancia de SL.
ENTRY_ZONE_MAX_SPAN = 0.01

#: Margen de la comparación de span. Un `<=` exacto rechazaría por un bit de coma
#: flotante un precio justo en el límite.
SPAN_EPS = 1e-9

#: Motivo de `entry_zone` cuando la zona está dentro del máximo permitido.
ZONE_OK = ""
#: No hay ninguna zona viva en el lado de la dirección (entrada a mercado).
ZONE_NO_AXIS = "no_zone_on_axis"
#: Hay zonas, pero todas están más lejos que el máximo permitido (entrada rechazada).
ZONE_TOO_FAR = "zone_too_far"


def _num(value: Any) -> Optional[float]:
    """A `float`, o `None` si no es convertible. Nunca lanza."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _spec_attr(spec: Any, nombre: str, default: Any = None) -> Any:
    """Un atributo del `SymbolSpec` por `getattr`, con default. `core` no lo importa."""
    if spec is None:
        return default
    return getattr(spec, nombre, default)


def max_zone_span(cfg: Optional[Dict[str, Any]] = None,
                  sl_distance: Optional[float] = None) -> float:
    """Distancia máxima precio -> zona de entrada, en unidades de precio.

    Múltiplo de la distancia de SL (portable entre mercados) y, si no se conoce
    ninguna, el absoluto histórico. `cfg["zone_max_span_sl_mult"]` deja ajustarlo.
    """
    c = dict(cfg or {})
    mult = _num(c.get("zone_max_span_sl_mult"))
    if mult is None or mult <= 0:
        mult = ENTRY_ZONE_MAX_SPAN_SL_MULT
    dist = _num(sl_distance)
    if dist is not None and dist > 0:
        return dist * float(mult)
    return float(ENTRY_ZONE_MAX_SPAN)


#: Origen de la distancia de SL (`structural_sl_and_tp`) -> etiqueta que ve la UI.
#: `None` (sin plan estructural, p. ej. sin `sl_dist`) cae al origen de la config.
_SL_SOURCE_LABELS = {
    "structure": "structural_atr",
    "atr": "atr_volatility",
    "config": "config_sl_min",
}


def _zonas(patterns: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Las FVG y OB vivas del snapshot, como cajas `{bottom, top, kind}`."""
    pat = patterns or {}
    out: List[Dict[str, Any]] = []
    for z in pat.get("fvgs") or []:
        box = _caja(z, "FVG")
        if box:
            out.append(box)
    for z in pat.get("order_blocks") or []:
        box = _caja(z, "OB")
        if box:
            out.append(box)
    return out


def _caja(z: Any, suffix: str) -> Optional[Dict[str, Any]]:
    """Una zona a `{bottom, top, kind}`, o `None` si no es una caja válida."""
    if not isinstance(z, dict):
        return None
    bottom, top = _num(z.get("bottom")), _num(z.get("top"))
    if bottom is None or top is None or bottom > top:
        return None
    kind = str(z.get("type") or "").strip() or suffix
    return {"bottom": bottom, "top": top, "kind": kind}


def determine_order_type(direction: str,
                         current_price: Optional[float],
                         entry_price: Optional[float],
                         has_zone: bool = True) -> str:
    """Tipo de orden: LIMIT para entrar en retroceso a la zona, MARKET si ya está dentro o sin zona."""
    dir_clean = str(direction or "").strip().upper()
    cp = _num(current_price)
    ep = _num(entry_price)
    if not has_zone or cp is None or ep is None:
        return dir_clean
    if dir_clean == "BUY":
        return "BUY_LIMIT" if cp > ep + 1e-6 else "BUY"
    if dir_clean == "SELL":
        return "SELL_LIMIT" if cp < ep - 1e-6 else "SELL"
    return dir_clean


def entry_zone(patterns: Optional[Dict[str, Any]],
               price: Optional[float],
               direction: str,
               max_span: Optional[float] = None) -> Dict[str, Any]:
    """Elige la zona (FVG/OB) más cercana donde entrar y explica si no hay.

    - BUY  -> la zona que queda POR DEBAJO del precio (rebote alcista).
    - SELL -> la zona que queda POR ENCIMA (rechazo bajista).

    Devuelve `{"zone": dict|None, "reason": str, "span": float|None}`. La zona trae
    `bottom`, `top`, `kind` y `price` (el borde de entrada: `bottom` en BUY, `top` en
    SELL). `span` es la distancia entre el precio y ese borde; `None` si no hay zona
    (o si el precio/zona no son números).
    """
    pat = patterns or {}
    p = _num(price)
    zonas = _zonas(pat)
    if p is None or not zonas:
        return {"zone": None, "reason": ZONE_NO_AXIS, "span": None}

    limit = ENTRY_ZONE_MAX_SPAN if max_span is None else float(max_span)
    buscando_buy = str(direction or "").strip().upper() != "SELL"
    if buscando_buy:
        candidatas = [
            z for z in zonas
            if "BULLISH" in z["kind"] and z["bottom"] <= p
        ]
        elegida = max(candidatas, key=lambda z: z["top"]) if candidatas else None
        if elegida is not None:
            borde = elegida["top"]
            if p <= elegida["top"]:
                span = 0.0
                entry_price = p
            else:
                span = p - borde
                entry_price = borde
    else:
        candidatas = [
            z for z in zonas
            if "BEARISH" in z["kind"] and z["top"] >= p
        ]
        elegida = min(candidatas, key=lambda z: z["bottom"]) if candidatas else None
        if elegida is not None:
            borde = elegida["bottom"]
            if p >= elegida["bottom"]:
                span = 0.0
                entry_price = p
            else:
                span = borde - p
                entry_price = borde

    if elegida is None:
        return {"zone": None, "reason": ZONE_NO_AXIS, "span": None}

    if span > limit + SPAN_EPS:
        return {"zone": None, "reason": ZONE_TOO_FAR, "span": span}

    return {
        "zone": {"bottom": elegida["bottom"], "top": elegida["top"],
                 "kind": elegida["kind"], "price": entry_price},
        "reason": ZONE_OK,
        "span": span,
    }


def plan_levels(entry: Optional[float],
                direction: str,
                sl_distance: Optional[float],
                tp_ratio: Optional[float] = None,
                digits: Optional[int] = None) -> Dict[str, Optional[float]]:
    """SL y TP a partir de la entrada, la distancia de SL y el múltiplo de R.

    `None` para `sl`/`tp` si falta la entrada o la distancia (con un número
    inventado el plan es una mentira). Los niveles se redondean a `digits` (los
    dígitos del símbolo) para que no salgan precios de 15 decimales que el bróker
    no acepta.
    """
    e, d = _num(entry), _num(sl_distance)
    if e is None or d is None or d <= 0:
        return {"entry": e, "sl": None, "tp": None}
    ratio = _num(tp_ratio)
    if ratio is None:
        ratio = DEFAULT_TP_RATIO
    nd = 6 if digits is None else max(0, min(8, int(digits)))
    if str(direction or "").strip().upper() == "SELL":
        sl, tp = e + d, e - d * ratio
    else:
        sl, tp = e - d, e + d * ratio
    return {"entry": round(e, nd), "sl": round(sl, nd), "tp": round(tp, nd)}


def killzone_forced_now(killzones: Optional[List[Dict[str, str]]] = None,
                        now: Optional[datetime] = None) -> Optional[datetime]:
    """Un instante dentro de la PRIMERA ventana configurada, para el modo test.

    `killzone=1` tiene que poner el score dentro de una sesión que el operador
    reconoce, o el flag de test no estaría probando lo que dice que prueba. Ancla al
    día de `now` a la hora de inicio (el borde, no el medio: `killzone_score` usa
    `>= inicio y < fin`, y el borde de inicio es "dentro" sin ambigüedad). `None` sin
    ventanas parseables.
    """
    for w in killzones or []:
        hhmm = str((w or {}).get("start") or "").strip()
        if ":" not in hhmm:
            continue
        try:
            hh, mm = (int(x) for x in hhmm.split(":")[:2])
        except (TypeError, ValueError):
            continue
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            base = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            return base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return None


def evaluate_gate(snapshot: Optional[Dict[str, Any]],
                  cfg: Optional[Dict[str, Any]] = None,
                  spec: Any = None,
                  min_score: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """El plan de setup de la mejor dirección, o `None` si el snapshot no vale.

    Elige la dirección de mayor score de `snapshot["risk_engine"]` y decide si el
    setup es OPERABLE. El veredicto exige (D-007): score >= umbral, zona alcanzable,
    distancia de SL conocida y `risk_engine.validate_entry` sin rechazo. La killzone
    NO es un AND: ya puntúa dentro del score, así que se publica (`in_killzone`,
    `killzone_name`) como información pero no se vuelve a exigir.

    `min_score` sobrescribe el umbral de la config para esta llamada; `None` usa
    `strategy.min_score_for(cfg)`. Quien llama publica el valor aplicado en
    `min_score_used`, porque un umbral distinto del de `strategy.yaml` no puede quedar
    solo en la petición.

    `None` si el snapshot no trae scores: no hay nada que evaluar, y un veredicto
    sobre datos que no existen sería inventado.
    """
    if not snapshot:
        return None
    re_ = snapshot.get("risk_engine") or {}
    lados = [(d, s) for d, s in (("BUY", re_.get("bull")), ("SELL", re_.get("bear"))) if s]
    if not lados:
        return None

    score_buy = float((re_.get("bull") or {}).get("score") or 0.0)
    score_sell = float((re_.get("bear") or {}).get("score") or 0.0)
    scores_empatados = (
        len(lados) == 2
        and abs(score_buy - score_sell) < 1e-6
        and score_buy > 0.0
    )

    direction, mejor = max(lados, key=lambda par: float((par[1] or {}).get("score") or 0.0))
    base_score = float((mejor or {}).get("score") or 0.0)
    components = (mejor or {}).get("components") or {}
    killzone = components.get("killzone") or {}
    in_killzone = bool(killzone.get("in_killzone"))
    killzone_name = killzone.get("name")

    if min_score is not None and _num(min_score) is not None:
        umbral = float(_num(min_score))
    else:
        umbral = strategy.min_score_for(cfg)

    symbol = str(snapshot.get("symbol") or "").upper()
    price = _num(snapshot.get("current_price"))
    analysis = snapshot.get("analysis") or {}
    patterns = (analysis.get("patterns")) or {}

    # Unificación de confluencia HTF dentro del gate de aprobación (P1 - Audit #4)
    htf_ctx = analysis.get("htf_context")
    htf_bonus = 0.0
    htf_conf = None
    if isinstance(htf_ctx, dict) and htf_ctx.get("timeframes"):
        try:
            htf_conf = smc_engine.evaluate_htf_confluence(
                htf_ctx, direction=direction, current_price=price
            )
            htf_bonus = float(htf_conf.get("score_bonus") or 0.0)
        except Exception:  # noqa: BLE001
            htf_conf = None
            htf_bonus = 0.0

    score_effective = round(min(100.0, max(0.0, base_score + htf_bonus)), 1)
    verdict = risk_engine.grade_score(score_effective)

    sl_dist, sl_origin = strategy.sl_distance_for(cfg, symbol or None, spec)
    tp_ratio = strategy.tp_ratio_for(cfg)
    span_limit = max_zone_span(cfg, sl_dist)

    zone = entry_zone(patterns, price, direction, span_limit)
    elegida = zone.get("zone")
    if elegida:
        # Invalidación estructural: borde distal de la zona de entrada elegida
        invalidation = float(elegida["bottom"]) if direction == "BUY" else float(elegida["top"])
    else:
        invalidation = _num((re_.get("invalidation") or {}).get(direction))

    entry = _num(elegida.get("price")) if elegida else None
    if entry is None:
        entry = price  # sin zona en eje: entrada a mercado al precio actual
    entry_kind = "market" if elegida is None else "zone"
    order_type = determine_order_type(direction, price, entry, has_zone=(elegida is not None))
    is_limit_order = order_type in ("BUY_LIMIT", "SELL_LIMIT")
    zone_too_far = zone.get("reason") == ZONE_TOO_FAR

    digits = _spec_attr(spec, "digits", 6)
    point = _spec_attr(spec, "point", 0.00001)

    atr_val = _num(analysis.get("atr_14")) or _num(snapshot.get("atr_14")) or 0.0
    spread_val = _num(snapshot.get("spread_price")) or 0.0
    # Imán de liquidez (M25): el nivel que publica `evaluate_htf_confluence` es
    # `next_liquidity`. Antes se buscaba `liquidity_magnet`, una clave que el
    # motor NO produce, así que el TP al imán solo existía en los tests.
    target_magnet = _num(
        ((analysis.get("htf_context") or {}).get("liquidity_magnet") or {}).get("price"))
    if target_magnet is None and isinstance(htf_conf, dict):
        target_magnet = _num((htf_conf.get("next_liquidity") or {}).get("price"))

    atr_min_mult = _num(cfg.get("structural_atr_min_mult")) if isinstance(cfg, dict) else None
    if atr_min_mult is None:
        atr_min_mult = float(strategy.DEFAULTS.get("structural_atr_min_mult", 1.0))

    distal_level = invalidation if elegida else None
    if sl_dist is None:
        sl = None
        tp = None
        effective_sl_dist = None
        effective_tp_dist = None
        struct_levels = {"is_structural": False, "magnet_applied": False}
    else:
        struct_levels = indicators.structural_sl_and_tp(
            entry=entry,
            direction=direction,
            distal=distal_level,
            atr=atr_val,
            spread=spread_val,
            tick_size=point,
            sl_min=sl_dist,
            tp_ratio=tp_ratio or DEFAULT_TP_RATIO,
            target_magnet=target_magnet,
            digits=digits,
            atr_min_mult=atr_min_mult,
        )
        sl = struct_levels["sl"]
        tp = struct_levels["tp"]
        effective_sl_dist = struct_levels["sl_distance"]
        effective_tp_dist = struct_levels["tp_distance"]

    validation: Dict[str, Any]
    try:
        validation = risk_engine.validate_entry(
            entry=entry, sl=sl, target=tp, direction=direction,
            current_price=price, invalidate_level=invalidation, cfg=cfg,
            tick_size=point,
        )
    except Exception as exc:  # noqa: BLE001 - un fallo del motor es un rechazo, no un 500
        validation = {"approved": False, "rejected": True,
                      "reasons": ["risk_engine: {0}".format(exc)]}

    approved = bool(
        score_effective >= umbral
        and sl_dist is not None
        and sl is not None
        and not zone_too_far
        and not scores_empatados
        and validation.get("approved")
    )

    reasons: List[str] = []
    if score_effective < umbral:
        if htf_bonus != 0.0:
            reasons.append(
                f"Score {score_effective:.1f} (base {base_score:.1f} + HTF {htf_bonus:+.1f}) < umbral {umbral:.0f}."
            )
        else:
            reasons.append(
                "Score {0:.1f} < umbral {1:.0f}.".format(score_effective, umbral))
    if scores_empatados:
        reasons.append(
            "Conflicto direccional: BUY y SELL empatan en score ({0:.1f}).".format(score_buy))
    if sl_dist is None:
        clave = "sl_distance_by_symbol.{0}".format(symbol) if symbol else "sl_distance"
        reasons.append(
            "Sin distancia de SL configurada para este símbolo ('{0}').".format(clave))
    if zone_too_far:
        reasons.append(
            "Zona de entrada a {0} del precio (max {1}).".format(
                _fmt(zone.get("span"), digits), _fmt(span_limit, digits)))
    reasons.extend(str(r) for r in (validation.get("reasons") or []))

    return {
        "symbol": symbol,
        "direction": direction,
        "order_type": order_type,
        "is_limit_order": is_limit_order,
        "score": score_effective,
        "base_score": base_score,
        "htf_score_bonus": htf_bonus,
        "htf_confluence": htf_conf,
        "verdict": verdict,
        "min_score_used": umbral,
        "killzone": {"in_killzone": in_killzone, "name": killzone_name},
        "current_price": price,
        "entry": entry,
        "entry_kind": entry_kind,
        "entry_zone": zone.get("zone"),
        "zone_reason": zone.get("reason"),
        "sl": sl,
        "tp": tp,
        "sl_distance": effective_sl_dist,
        "sl_distance_source": _SL_SOURCE_LABELS.get(
            struct_levels.get("dist_source") or "", sl_origin),
        "tp_ratio_r": tp_ratio if tp_ratio is not None else DEFAULT_TP_RATIO,
        "invalidate_level": invalidation,
        "unit_label": str(_spec_attr(spec, "unit_label", "") or ""),
        "approved": approved,
        "reasons": reasons,
        "is_structural_sl": struct_levels["is_structural"],
        "atr_14": atr_val,
        "atr_floor": struct_levels.get("atr_floor"),
        "magnet_applied": struct_levels["magnet_applied"],
    }


def _fmt(value: Any, digits: Optional[int] = None) -> str:
    """Un número para un mensaje de motivo, sin ceros de cola."""
    n = _num(value)
    if n is None:
        return "-"
    nd = 6 if digits is None else max(0, min(8, int(digits)))
    return "{0:.{1}f}".format(n, nd).rstrip("0").rstrip(".")


__all__ = [
    "DEFAULT_TP_RATIO",
    "ENTRY_ZONE_MAX_SPAN",
    "ENTRY_ZONE_MAX_SPAN_SL_MULT",
    "SPAN_EPS",
    "ZONE_NO_AXIS",
    "ZONE_OK",
    "ZONE_TOO_FAR",
    "determine_order_type",
    "entry_zone",
    "evaluate_gate",
    "killzone_forced_now",
    "max_zone_span",
    "plan_levels",
]
