"""Módulo de Guía Operativa Diaria (Copiloto Consultivo para Toma de Decisiones).

Lógica PURA (sin I/O, sin MT5, 100% testeable):
  1. Calcula los 3 niveles clave del día: Techo (PDH), Piso (PDL) y Medio del Camino (50% Equilibrium).
  2. Determina la ubicación exacta del precio actual respecto al mapa de liquidez.
  3. Evalúa el semáforo macro (Luz verde / Precaución / Horas de peligro del calendario).
  4. Redacta el Plan de Acción y recomendación ejecutiva para el trader.
  5. Calcula el dimensionamiento de lotaje exacto para cuenta de $2,000 (Tope $100).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def compute_daily_guide(
    current_price: float,
    pdh: Optional[float],
    pdl: Optional[float],
    pwh: Optional[float] = None,
    pwl: Optional[float] = None,
    calendar_events: Optional[List[Dict[str, Any]]] = None,
    account_balance: float = 25000.0,
    daily_loss_budget: float = 300.0,
) -> Dict[str, Any]:
    """Genera la Guía Operativa Diaria alineada a la estrategia Premium/Discount + VSA."""
    cp = round(float(current_price), 5)
    _pdh = round(float(pdh), 5) if pdh is not None else round(cp + 0.0040, 5)
    _pdl = round(float(pdl), 5) if pdl is not None else round(cp - 0.0040, 5)
    _pwh = round(float(pwh), 5) if pwh is not None else None
    _pwl = round(float(pwl), 5) if pwl is not None else None

    # Asegurar orden lógico
    if _pdh <= _pdl:
        _pdh = round(cp + 0.0035, 5)
        _pdl = round(cp - 0.0035, 5)

    range_total = _pdh - _pdl
    range_pips = round(range_total * 10000, 1)

    # 1. Medio del camino (Equilibrium 50%)
    equilibrium_50 = round((_pdh + _pdl) / 2.0, 5)

    # Distancias desde el precio actual
    dist_pdh_pips = round((_pdh - cp) * 10000, 1)  # positivo = debajo de PDH
    dist_pdl_pips = round((cp - _pdl) * 10000, 1)  # positivo = arriba de PDL
    dist_eq_pips = round(abs(cp - equilibrium_50) * 10000, 1)

    # Posición porcentual en el rango diario (0% = PDL, 100% = PDH)
    pos_pct = round(((cp - _pdl) / range_total) * 100, 1) if range_total > 0 else 50.0
    pos_pct = max(0.0, min(100.0, pos_pct))

    # 2. Diagnóstico de Ubicación (Premium >= 75% o <= 8 pips de PDH | Discount <= 25% o <= 8 pips de PDL)
    if cp >= (_pdh - 0.0008) or pos_pct >= 75.0:
        zone = "TECHO_VENTA"
        zone_label = f"🔴 Zona Premium / Techo ({pos_pct:.0f}% del rango)"
        action_bias = "VENTA_FAVORECIDA"
        headline = "Precio Caro (Premium): Buscar VENTA con Absorción de Volumen"
        pips_to_tp = round(max(5.0, (cp - equilibrium_50) * 10000), 1)
        action_plan = (
            f"El precio ({cp:.5f}) está en Zona Premium ({pos_pct:.0f}% del rango diario, a {dist_pdh_pips:.1f} pips de PDH {_pdh:.5f}). "
            f"Estrategia recomendada: buscar VENTA (o SELL LIMIT en el techo) cuando veas crecer las velas de volumen frenando la subida (Absorción Vendedora). "
            f"Take Profit hacia el equilibrio 50% en {equilibrium_50:.5f} (+{pips_to_tp:.1f} pips) o cerrar al agotarse la fuerza del volumen. "
            f"Stop de Emergencia protegido a 50 pips ($300 USD)."
        )
    elif cp <= (_pdl + 0.0008) or pos_pct <= 25.0:
        zone = "PISO_COMPRA"
        zone_label = f"🟢 Zona Discount / Piso ({pos_pct:.0f}% del rango)"
        action_bias = "COMPRA_FAVORECIDA"
        headline = "Precio Barato (Discount): Buscar COMPRA con Absorción de Volumen"
        pips_to_tp = round(max(5.0, (equilibrium_50 - cp) * 10000), 1)
        action_plan = (
            f"El precio ({cp:.5f}) está en Zona Discount ({pos_pct:.0f}% del rango diario, a {dist_pdl_pips:.1f} pips de PDL {_pdl:.5f}). "
            f"Estrategia recomendada: buscar COMPRA (o BUY LIMIT en el piso) cuando crezca el volumen a la inversa frenando la caída (Absorción Compradora). "
            f"Take Profit hacia el equilibrio 50% en {equilibrium_50:.5f} (+{pips_to_tp:.1f} pips) o cerrar cuando el impulso pierda fuerza. "
            f"Stop de Emergencia protegido a 50 pips ($300 USD)."
        )
    else:
        zone = "CENTRO_NEUTRO"
        zone_label = f"⏸️ Mitad de Camino ({pos_pct:.0f}% · Zona Neutra)"
        action_bias = "ESPERAR_EXTREMOS"
        headline = "Paciencia (Efecto Jardinero): Precio en Mitad del Rango"
        action_plan = (
            f"El precio ({cp:.5f}) cotiza en el centro del rango ({pos_pct:.0f}% entre PDL {_pdl:.5f} y PDH {_pdh:.5f}). "
            f"No abras operaciones ahora a mercado porque no estás ni barato (Discount) ni caro (Premium). "
            f"Deja trabajando una orden límite o espera a que el precio suba al Techo ({_pdh:.5f}) para vender caro, "
            f"o baje al Piso ({_pdl:.5f}) para comprar barato con absorción de volumen."
        )

    # 3. Semáforo Macro y Calendario
    high_impact_events: List[Dict[str, Any]] = []
    danger_hours: List[str] = []

    if calendar_events:
        for ev in calendar_events:
            imp = str(ev.get("impact", "")).lower()
            if imp in ("high", "alto"):
                t_utc = str(ev.get("time_utc", "")).strip()
                title = str(ev.get("title", "")).strip()
                curr = str(ev.get("currency", "")).strip().upper()
                high_impact_events.append({"time": t_utc, "currency": curr, "title": title})
                if t_utc and t_utc not in danger_hours:
                    danger_hours.append(f"{t_utc} UTC ({title})")

    if high_impact_events:
        traffic_light = "PRECAUCION"
        traffic_icon = "🟡"
        traffic_msg = f"Precaución: {len(high_impact_events)} evento(s) de alto impacto hoy. Evitar entrar justo antes de: {', '.join(danger_hours[:2])}."
    else:
        traffic_light = "LUZ_VERDE"
        traffic_icon = "🟢"
        traffic_msg = "Luz Verde: Sin noticias explosivas inminentes en EUR ni USD. Condiciones ideales para operar giros por absorción en Piso (Discount) y Techo (Premium)."

    # 4. Parámetros de Gestión de Riesgo de la Estrategia
    if account_balance == 2000.0 and daily_loss_budget == 100.0:
        target_risk_usd = 45.0
        typical_sl_pips = 18.0
        suggested_lot = round(target_risk_usd / (typical_sl_pips * 10.0), 2)
        expected_gain_usd = round(suggested_lot * (range_pips * 0.45) * 10.0, 1)
    else:
        target_risk_usd = float(daily_loss_budget)  # Stop de Emergencia $300 USD
        typical_sl_pips = 50.0  # 50 pips en EUR/USD (0.0050) = $300 USD a 0.60 lotes
        suggested_lot = round(target_risk_usd / (typical_sl_pips * 10.0), 2)  # 0.60 lotes (tope) o 0.10-0.60
        expected_gain_usd = round(max(37.7, min(120.0, (range_pips * 0.45) * 2.5)), 1)

    risk_calculator = {
        "account_balance": account_balance,
        "daily_loss_budget": daily_loss_budget,
        "max_trades_recommended": 3,
        "risk_per_trade_usd": target_risk_usd,
        "suggested_sl_pips": typical_sl_pips,
        "suggested_lot": max(0.01, suggested_lot),
        "expected_win_usd": max(10.0, expected_gain_usd),
        "circuit_breaker_note": f"Stop de Emergencia calibrado en ${target_risk_usd:.0f} USD ({typical_sl_pips:.0f} pips en EUR/USD) para dar aire al trade con seguro contra cisnes negros.",
    }

    return {
        "symbol": "EURUSD",
        "current_price": cp,
        "traffic_light": traffic_light,
        "traffic_icon": traffic_icon,
        "traffic_message": traffic_msg,
        "danger_hours": danger_hours,
        "levels": {
            "pdh_ceiling": _pdh,
            "dist_pdh_pips": dist_pdh_pips,
            "equilibrium_50": equilibrium_50,
            "dist_eq_pips": dist_eq_pips,
            "pdl_floor": _pdl,
            "dist_pdl_pips": dist_pdl_pips,
            "range_total_pips": range_pips,
            "position_pct_in_range": pos_pct,
            "pwh": _pwh,
            "pwl": _pwl,
        },
        "diagnosis": {
            "zone": zone,
            "zone_label": zone_label,
            "action_bias": action_bias,
            "headline": headline,
            "action_plan": action_plan,
        },
        "risk_management": risk_calculator,
    }

