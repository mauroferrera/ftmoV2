"""Tests de `core.setup_gate`: zona de entrada, niveles y veredicto.

El gate es puro: se le pasa un snapshot ya armado y la config plana. Aquí se fijan
los contratos que el watcher y las rutas esperan:

  - Elige la dirección de mayor score, no una fijada de antemano.
  - La zona de entrada es la más CERCANA del lado correcto (debajo en BUY, encima
    en SELL) y se descarta si está más lejos que el máximo permitido.
  - El plan (entry/sl/tp) sale de la distancia de SL configurada y del `tp_ratio_r`.
  - La killzone NO es un AND: un score alto fuera de ventana sigue aprobando (D-007).
  - Sin snapshot o sin scores, `None`: no se inventa un veredicto.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core import risk_engine, setup_gate


def _patrones_favor(direccion: str):
    """Un juego de patrones a favor de la dirección (FVG + OB + sweep)."""
    alcista = direccion.upper() == "BUY"
    return {
        "fvgs": [{"type": "BULLISH_FVG" if alcista else "BEARISH_FVG",
                  "bottom": 1.1000 if alcista else 1.1010, "top": 1.1005 if alcista else 1.1015}],
        "order_blocks": [{"type": "BULLISH_OB" if alcista else "BEARISH_OB",
                          "bottom": 1.0995 if alcista else 1.1010, "top": 1.1005 if alcista else 1.1020}],
        "sweeps": [{"type": "PDL_SWEEP" if alcista else "PDH_SWEEP"}],
    }


def _snapshot(direccion="BUY", price=1.1010, now=None, killzones=None):
    patrones = _patrones_favor(direccion)
    re_ = risk_engine.score_directions(
        patrones, {"smc": 85, "killzone": 15},
        killzones if killzones is not None else risk_engine.DEFAULT_KILLZONES,
        now or datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
    )
    return {"symbol": "EURUSD", "current_price": price,
            "analysis": {"patterns": patrones}, "risk_engine": re_}


CFG = {"sl_distance_by_symbol": {"EURUSD": 0.0012}, "tp_ratio_r": 2.0, "min_score": 0.0}


# ---------------------------------------------------------------------------
# entry_zone
# ---------------------------------------------------------------------------


class TestEntryZone:
    def test_buy_elige_la_zona_mas_cercana_por_debajo(self):
        patrones = {
            "fvgs": [
                {"type": "BULLISH_FVG", "bottom": 1.0900, "top": 1.0905},
                {"type": "BULLISH_FVG", "bottom": 1.0990, "top": 1.0995},
            ],
            "order_blocks": [],
            "sweeps": [],
        }
        out = setup_gate.entry_zone(patrones, 1.1000, "BUY", max_span=0.01)
        assert out["zone"]["price"] == 1.0995
        assert out["reason"] == setup_gate.ZONE_OK

    def test_buy_ignora_zonas_bearish(self):
        patrones = {
            "fvgs": [
                {"type": "BEARISH_FVG", "bottom": 1.0996, "top": 1.0998},
                {"type": "BULLISH_FVG", "bottom": 1.0990, "top": 1.0995},
            ],
            "order_blocks": [],
            "sweeps": [],
        }
        out = setup_gate.entry_zone(patrones, 1.1000, "BUY", max_span=0.01)
        assert out["zone"]["price"] == 1.0995
        assert out["zone"]["kind"] == "BULLISH_FVG"

    def test_sell_elige_la_zona_mas_cercana_por_encima(self):
        patrones = {
            "fvgs": [
                {"type": "BEARISH_FVG", "bottom": 1.1100, "top": 1.1105},
                {"type": "BEARISH_FVG", "bottom": 1.1010, "top": 1.1015},
            ],
            "order_blocks": [],
            "sweeps": [],
        }
        out = setup_gate.entry_zone(patrones, 1.1000, "SELL", max_span=0.01)
        assert out["zone"]["price"] == 1.1010

    def test_zona_demasiado_lejos_se_rechaza(self):
        patrones = {"fvgs": [{"type": "BULLISH_FVG", "bottom": 1.0000, "top": 1.0005}],
                    "order_blocks": [], "sweeps": []}
        out = setup_gate.entry_zone(patrones, 1.1000, "BUY", max_span=0.01)
        assert out["zone"] is None
        assert out["reason"] == setup_gate.ZONE_TOO_FAR

    def test_sin_zona_en_el_eje(self):
        out = setup_gate.entry_zone({"fvgs": [], "order_blocks": [], "sweeps": []},
                                    1.1000, "BUY", max_span=0.01)
        assert out["zone"] is None
        assert out["reason"] == setup_gate.ZONE_NO_AXIS


# ---------------------------------------------------------------------------
# max_zone_span / plan_levels
# ---------------------------------------------------------------------------


class TestPlanLevels:
    def test_buy_sl_debajo_tp_arriba(self):
        out = setup_gate.plan_levels(1.1000, "BUY", 0.0012, 2.0, digits=5)
        assert out["sl"] == 1.0988
        assert out["tp"] == 1.1024

    def test_sell_espejo(self):
        out = setup_gate.plan_levels(1.1000, "SELL", 0.0012, 2.0, digits=5)
        assert out["sl"] == 1.1012
        assert out["tp"] == 1.0976

    def test_sin_distancia_no_hay_niveles(self):
        out = setup_gate.plan_levels(1.1000, "BUY", None, 2.0, digits=5)
        assert out["sl"] is None and out["tp"] is None

    def test_span_es_multiplo_de_sl(self):
        assert setup_gate.max_zone_span(CFG, 0.0012) == 0.0012 * 0.5

    def test_span_absoluto_sin_sl(self):
        assert setup_gate.max_zone_span({}, None) == setup_gate.ENTRY_ZONE_MAX_SPAN


# ---------------------------------------------------------------------------
# evaluate_gate
# ---------------------------------------------------------------------------


class TestEvaluateGate:
    def test_setup_limpio_aprueba(self):
        plan = setup_gate.evaluate_gate(_snapshot(), CFG)
        assert plan["direction"] == "BUY"
        assert plan["approved"] is True
        assert plan["entry"] == 1.1005
        assert plan["sl"] == 1.0993 and plan["tp"] == 1.1029
        assert plan["killzone"]["in_killzone"] is True

    def test_killzone_no_es_un_and(self):
        """Fuera de ventana el score baja, pero un umbral 0 sigue aprobando (D-007)."""
        fuera = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
        plan = setup_gate.evaluate_gate(_snapshot(now=fuera), CFG)
        assert plan["killzone"]["in_killzone"] is False
        assert plan["approved"] is True

    def test_umbral_alto_rechaza_por_score(self):
        cfg = dict(CFG, min_score=99.0)
        plan = setup_gate.evaluate_gate(_snapshot(now=datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)), cfg)
        assert plan["approved"] is False
        assert any("umbral" in r for r in plan["reasons"])
        assert plan["min_score_used"] == 99.0

    def test_sin_sl_configurado_rechaza(self):
        plan = setup_gate.evaluate_gate(_snapshot(), {"tp_ratio_r": 2.0, "min_score": 0.0})
        assert plan["approved"] is False
        assert plan["sl"] is None

    def test_sin_snapshot_devuelve_none(self):
        assert setup_gate.evaluate_gate(None, CFG) is None

    def test_sin_scores_devuelve_none(self):
        assert setup_gate.evaluate_gate({"symbol": "EURUSD"}, CFG) is None

    def test_el_umbral_por_parametro_manda(self):
        plan = setup_gate.evaluate_gate(_snapshot(), CFG, min_score=0.0)
        assert plan["min_score_used"] == 0.0

    def test_invalidation_level_toma_distal_de_zona_elegida(self):
        snap = _snapshot(direccion="BUY", price=1.1010)
        # La zona FVG elegida es bottom 1.1000, top 1.1005
        plan = setup_gate.evaluate_gate(snap, CFG)
        assert plan["entry"] == 1.1005  # Proximal
        assert plan["invalidate_level"] == 1.1000  # Distal

    def test_scores_empatados_rechaza_por_conflicto(self):
        snap = _snapshot(direccion="BUY", price=1.1010)
        # Forzar empate de scores entre bull y bear
        snap["risk_engine"]["bear"] = dict(snap["risk_engine"]["bull"])
        plan = setup_gate.evaluate_gate(snap, CFG)
        assert plan["approved"] is False
        assert any("conflicto" in r.lower() for r in plan["reasons"])

    def test_killzone_decide_con_peso_real_dentro_vs_fuera(self):
        """Con pesos 80/20 y umbral 59.5, la killzone DECIDE activamente (P1 - Audit #3)."""
        cfg = dict(CFG, min_score=59.5)
        # 1. Dentro de Londres (08:00 UTC) -> SMC 0.70 * 80 + KZ 1.0 * 20 = 76.0 -> Aprobado
        dentro = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        snap_in = _snapshot(direccion="BUY", price=1.1010, now=dentro)
        # Forzar un patrón con SMC = 0.7 (FVG + OB)
        pat_70 = {
            "fvgs": [{"type": "BULLISH_FVG", "bottom": 1.1000, "top": 1.1005, "freshness": 0.75}],
            "order_blocks": [{"type": "BULLISH_OB", "bottom": 1.0990, "top": 1.0995, "freshness": 0.75}],
            "sweeps": [],
        }
        re_in = risk_engine.score_directions(pat_70, {"smc": 80, "killzone": 20}, risk_engine.DEFAULT_KILLZONES, dentro)
        snap_in["risk_engine"] = re_in
        snap_in["analysis"]["patterns"] = pat_70
        plan_in = setup_gate.evaluate_gate(snap_in, cfg)
        assert plan_in["score"] == 76.0
        assert plan_in["approved"] is True

        # 2. Fuera de Killzone (03:00 UTC) -> SMC 0.70 * 80 + KZ 0.0 * 20 = 56.0 -> Rechazado (< 59.5)
        fuera = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
        snap_out = _snapshot(direccion="BUY", price=1.1010, now=fuera)
        re_out = risk_engine.score_directions(pat_70, {"smc": 80, "killzone": 20}, risk_engine.DEFAULT_KILLZONES, fuera)
        snap_out["risk_engine"] = re_out
        snap_out["analysis"]["patterns"] = pat_70
        plan_out = setup_gate.evaluate_gate(snap_out, cfg)
        assert plan_out["score"] == 56.0
        assert plan_out["approved"] is False
        assert any("Score 56.0 < umbral 60" in r for r in plan_out["reasons"])

    def test_confluencia_htf_unificada_aprueba_con_bonus(self):
        """El bonus HTF se suma al score evaluado por el gate (P1 - Audit #4)."""
        cfg = dict(CFG, min_score=60.0)
        snap = _snapshot(direccion="BUY", price=1.1010)
        # Forzar base_score = 56.0
        snap["risk_engine"]["bull"]["score"] = 56.0
        # Simular htf_context con bonus de +10
        snap["analysis"]["htf_context"] = {
            "timeframes": {
                "D1": {"bias": "ALCISTA", "location": "Discount"},
                "H4": {"bias": "ALCISTA", "location": "Discount"},
            },
            "liquidity_magnet": {"label": "PDH", "price": 1.1050},
        }
        plan = setup_gate.evaluate_gate(snap, cfg)
        # Score efectivo = 56.0 + 11.0 = 67.0 >= 60.0 -> Aprobado!
        assert plan["base_score"] == 56.0
        assert plan["htf_score_bonus"] == 11.0
        assert plan["score"] == 67.0
        assert plan["approved"] is True

    def test_confluencia_htf_unificada_rechaza_con_penalizacion(self):
        """La penalización HTF reduce el score del gate y puede rechazar (P1 - Audit #4)."""
        cfg = dict(CFG, min_score=60.0)
        snap = _snapshot(direccion="BUY", price=1.1010)
        # Forzar base_score = 64.0 (que pasaría solo)
        snap["risk_engine"]["bull"]["score"] = 64.0
        # Simular htf_context en contra (bias bajista en Premium) -> penalización -4.5 pts
        snap["analysis"]["htf_context"] = {
            "timeframes": {
                "D1": {"bias": "BAJISTA", "location": "Premium"},
                "H4": {"bias": "BAJISTA", "location": "Premium"},
            },
            "liquidity_magnet": {"label": "PDL", "price": 1.0950},
        }
        plan = setup_gate.evaluate_gate(snap, cfg)
        # Score efectivo = 64.0 - 4.5 = 59.5 < 60.0 -> Rechazado!
        assert plan["base_score"] == 64.0
        assert plan["htf_score_bonus"] == -4.5
        assert plan["score"] == 59.5
        assert plan["approved"] is False
        assert any("Score 59.5 (base 64.0 + HTF -4.5) < umbral 60" in r for r in plan["reasons"])

    def test_order_type_limit_en_retroceso_a_zona(self):
        """Si el precio está extendido por encima de la zona BUY, el gate publica BUY_LIMIT."""
        snap = _snapshot(direccion="BUY", price=1.1010)
        # Zona FVG elegida: bottom 1.1000, top 1.1005. Precio actual: 1.1010
        plan = setup_gate.evaluate_gate(snap, CFG)
        assert plan["entry"] == 1.1005
        assert plan["order_type"] == "BUY_LIMIT"
        assert plan["is_limit_order"] is True

        # Si el precio ya está mitigando/dentro de la zona (1.1002), es BUY a mercado
        snap_inside = _snapshot(direccion="BUY", price=1.1002)
        plan_inside = setup_gate.evaluate_gate(snap_inside, CFG)
        assert plan_inside["order_type"] == "BUY"
        assert plan_inside["is_limit_order"] is False

    def test_premium_discount_bloquea_venta_en_piso_y_compra_en_techo(self):
        """Regla 1: Prohibido vender en Discount (piso) y prohibido comprar en Premium (techo)."""
        # 1. Intento de SELL en el piso (pdl=1.1000, pdh=1.1100, price=1.1010 -> 10% Discount)
        snap_sell_floor = _snapshot(direccion="SELL", price=1.1010)
        snap_sell_floor["pdh"] = 1.1100
        snap_sell_floor["pdl"] = 1.1000
        plan_floor = setup_gate.evaluate_gate(snap_sell_floor, CFG)
        assert plan_floor["dealing_zone"] == "DISCOUNT"
        assert plan_floor["approved"] is False
        assert any("prohibido vender en el piso" in r for r in plan_floor["reasons"])

        # 2. Intento de BUY en el techo (pdl=1.0900, pdh=1.1020, price=1.1010 -> 92% Premium)
        snap_buy_ceil = _snapshot(direccion="BUY", price=1.1010)
        snap_buy_ceil["pdh"] = 1.1020
        snap_buy_ceil["pdl"] = 1.0900
        plan_ceil = setup_gate.evaluate_gate(snap_buy_ceil, CFG)
        assert plan_ceil["dealing_zone"] == "PREMIUM"
        assert plan_ceil["approved"] is False
        assert any("prohibido comprar en el techo" in r for r in plan_ceil["reasons"])

        # 3. BUY en el piso (Discount 10%) -> Aprobado y recibe bonus de ubicación (+10 pts)
        snap_buy_floor = _snapshot(direccion="BUY", price=1.1010)
        snap_buy_floor["pdh"] = 1.1100
        snap_buy_floor["pdl"] = 1.1000
        plan_buy_ok = setup_gate.evaluate_gate(snap_buy_floor, CFG)
        assert plan_buy_ok["dealing_zone"] == "DISCOUNT"
        assert plan_buy_ok["location_score_bonus"] == 10.0
        assert plan_buy_ok["approved"] is True



