"""La política de salida en dos tramos, SIN bróker, sin base, sin FastAPI.

Todo el dinero que mueve la gestión parcial se decide aquí: el volumen del
primer tramo, el break-even, el ratchet y el nivel del runner. Un error de
redondeo en `partial_close_volume` no es un detalle de test: es cerrar (o no)
lotes de más en una cuenta de fondeo. Por eso los números se prueban uno a uno.

La pureza la vigila `tests/unit/test_core_purity.py`: este módulo importa solo
`core`, y así tiene que seguir.
"""

from __future__ import annotations

import pytest

from core import exit_policy
from core.strategy import DEFAULTS


# ---------------------------------------------------------------------------
# Config: ratios y offsets
# ---------------------------------------------------------------------------


def test_ratios_default():
    assert exit_policy.partial_ratio_for(None) == float(DEFAULTS["partial_ratio_r"])
    assert exit_policy.runner_ratio_for(None) == float(DEFAULTS["runner_ratio_r"])


def test_ratios_desde_config_planay_anidada():
    plano = {"partial_ratio_r": 1.5, "runner_ratio_r": 4.0}
    anidada = {"execution": {"partial_ratio_r": 1.5, "runner_ratio_r": 4.0}}
    for cfg in (plano, anidada):
        assert exit_policy.partial_ratio_for(cfg) == 1.5
        assert exit_policy.runner_ratio_for(cfg) == 4.0


@pytest.mark.parametrize("malo", [0, -1, "x", True, None])
def test_ratio_invalido_cae_al_default(malo):
    """Un ratio de 0 haría que TODO esté a la vez a 1:2R y a 1:5R."""
    assert exit_policy.partial_ratio_for({"partial_ratio_r": malo}) == float(
        DEFAULTS["partial_ratio_r"])


def test_partial_volume_pct_default_y_limites():
    assert exit_policy.partial_volume_pct_for(None) == float(
        DEFAULTS["partial_volume_pct"])
    assert exit_policy.partial_volume_pct_for({"partial_volume_pct": 40}) == 40.0
    assert exit_policy.partial_volume_pct_for({"partial_volume_pct": 0}) == float(
        DEFAULTS["partial_volume_pct"])
    assert exit_policy.partial_volume_pct_for({"partial_volume_pct": 150}) == float(
        DEFAULTS["partial_volume_pct"])


def test_offset_por_simbolo_manda_sobre_el_global():
    cfg = {"breakeven_offset": 0.00005,
           "breakeven_offset_by_symbol": {"XAUUSD": 0.30}}
    assert exit_policy.breakeven_offset_for(cfg, "XAUUSD") == 0.30
    assert exit_policy.breakeven_offset_for(cfg, "EURUSD") == 0.00005
    assert exit_policy.breakeven_offset_for(cfg, None) == 0.00005
    assert exit_policy.breakeven_offset_for(None, "XAUUSD") == float(
        DEFAULTS["breakeven_offset"])


def test_offset_negativo_cae_al_default():
    cfg = {"breakeven_offset": -1.0}
    assert exit_policy.breakeven_offset_for(cfg, None) == float(
        DEFAULTS["breakeven_offset"])


# ---------------------------------------------------------------------------
# R original y R actual
# ---------------------------------------------------------------------------


def test_r_original_buy():
    assert exit_policy.r_original_from(1.09912, 1.10012) == pytest.approx(0.00100)


def test_r_original_sell():
    assert exit_policy.r_original_from(1.10112, 1.10012) == pytest.approx(0.00100)


@pytest.mark.parametrize("sl,open_", [(0, 1.10), (None, 1.10), (1.10, 0),
                                      (1.10, None), (1.10, 1.10)])
def test_r_original_inmedible_es_none(sl, open_):
    """Un SL a 0 es "sin stop"; un SL ya en BE da ~0. Ninguno es un R."""
    assert exit_policy.r_original_from(sl, open_) is None


def test_r_actual_signos():
    r0 = 0.00100
    assert exit_policy.r_actual("BUY", 1.10012, 1.10212, r0) == pytest.approx(2.0)
    assert exit_policy.r_actual("BUY", 1.10012, 1.09912, r0) == pytest.approx(-1.0)
    assert exit_policy.r_actual("SELL", 1.10012, 1.09812, r0) == pytest.approx(2.0)
    assert exit_policy.r_actual("SELL", 1.10012, 1.10212, r0) == pytest.approx(-2.0)


@pytest.mark.parametrize("r0", [None, 0, 0.0, -1])
def test_r_actual_sin_base_es_none(r0):
    assert exit_policy.r_actual("BUY", 1.10, 1.11, r0) is None


# ---------------------------------------------------------------------------
# Volumen del parcial: floor a step, mínimos del bróker
# ---------------------------------------------------------------------------


def test_parcial_medio():
    res = exit_policy.partial_close_volume(1.00, 50.0, 0.01, 0.01)
    assert res["volume"] == pytest.approx(0.50)
    assert res["remaining"] == pytest.approx(0.50)
    assert res["reason"] is None


def test_parcial_con_step_gran_no_redondea_arriba():
    """0.50 de step 0.05: floor exacto. 0.52 pediría 0.52 y sale 0.50."""
    res = exit_policy.partial_close_volume(1.04, 50.0, 0.01, 0.05)
    assert res["volume"] == pytest.approx(0.50)
    assert res["remaining"] == pytest.approx(0.50)


def test_parcial_step_imperfecto_nunca_cierra_mas_de_lo_pedido():
    """2.50 lotes al 50% con step 0.07: floor(12.5/7)*0.07 = 1.19, no 1.26."""
    res = exit_policy.partial_close_volume(2.50, 50.0, 0.01, 0.07)
    assert res["volume"] == pytest.approx(1.19)
    assert res["volume"] <= 1.25


def test_parcial_rechaza_si_el_tramo_baja_del_minimo():
    """0.03 al 50% con step 0.01 da 0.01: es el mínimo, PERO el resto 0.02
    también tiene que sobrevivir; 0.02/0.01 sí. El caso que SÍ se rechaza es
    un tramo por debajo del mínimo."""
    res = exit_policy.partial_close_volume(0.01, 50.0, 0.01, 0.01)
    assert res["volume"] is None
    assert "mínimo" in res["reason"]


def test_parcial_rechaza_si_el_resto_baja_del_minimo():
    """0.04 lotes al 75% con mínimo 0.02: se cerrarían 0.03 (válido), pero el
    resto (0.01) queda por debajo del mínimo: no se parte la posición."""
    res = exit_policy.partial_close_volume(0.04, 75.0, 0.02, 0.01)
    assert res["volume"] is None
    assert "resto" in res["reason"]


def test_parcial_sin_volumen():
    res = exit_policy.partial_close_volume(None, 50.0, 0.01, 0.01)
    assert res["volume"] is None
    assert res["reason"]


def test_parcial_pct_invalido_cae_al_default():
    res = exit_policy.partial_close_volume(1.0, 0.0, 0.01, 0.01)
    assert res["volume"] == pytest.approx(
        1.0 * float(DEFAULTS["partial_volume_pct"]) / 100.0)


# ---------------------------------------------------------------------------
# Break-even, ratchet y runner
# ---------------------------------------------------------------------------


def test_breakeven_buy_sell():
    assert exit_policy.breakeven_sl("BUY", 1.10012, 0.00005) == pytest.approx(1.10017)
    assert exit_policy.breakeven_sl("SELL", 1.10012, 0.00005) == pytest.approx(1.10007)


def test_breakeven_xau_offset_grande():
    assert exit_policy.breakeven_sl("BUY", 2650.00, 0.30) == pytest.approx(2650.30)


def test_breakeven_sin_entrada_es_none():
    assert exit_policy.breakeven_sl("BUY", 0, 0.00005) is None
    assert exit_policy.breakeven_sl("BUY", None, 0.00005) is None


def test_ratchet_buy_no_afloja():
    """El propuesto peor (más abajo) se descarta; queda el actual."""
    assert exit_policy.ratchet_sl("BUY", 1.10050, 1.10017) == pytest.approx(1.10050)


def test_ratchet_buy_mejora():
    assert exit_policy.ratchet_sl("BUY", 1.09912, 1.10017) == pytest.approx(1.10017)


def test_ratchet_sell_no_afloja():
    """Para SELL, stop más BAJO es mejor: el propuesto más alto se descarta."""
    assert exit_policy.ratchet_sl("SELL", 1.09970, 1.10007) == pytest.approx(1.09970)


def test_ratchet_sell_mejora():
    assert exit_policy.ratchet_sl("SELL", 1.10112, 1.10007) == pytest.approx(1.10007)


def test_ratchet_sin_actual_acepta_el_propuesto():
    assert exit_policy.ratchet_sl("BUY", 0, 1.10017) == pytest.approx(1.10017)
    assert exit_policy.ratchet_sl("BUY", None, 1.10017) == pytest.approx(1.10017)


def test_ratchet_sin_propuesto_conserva_el_actual():
    assert exit_policy.ratchet_sl("BUY", 1.10050, None) == pytest.approx(1.10050)


def test_runner_level():
    r0 = 0.00100
    assert exit_policy.runner_level("BUY", 1.10012, r0, 5.0) == pytest.approx(1.10512)
    assert exit_policy.runner_level("SELL", 1.10012, r0, 5.0) == pytest.approx(1.09512)


def test_runner_level_sin_r_original_es_none():
    assert exit_policy.runner_level("BUY", 1.10012, None, 5.0) is None
    assert exit_policy.runner_level("BUY", 1.10012, 0.0, 5.0) is None


# ---------------------------------------------------------------------------
# management_view: la consulta que ve el panel
# ---------------------------------------------------------------------------


def _pos(**kw):
    base = {"ticket": 42, "symbol": "EURUSD", "type": "BUY",
            "volume": 1.00, "price_open": 1.10012, "price_current": 1.10212,
            "sl": 1.09912, "tp": 0.0}
    base.update(kw)
    return base


CFG = {"partial_ratio_r": 2.0, "runner_ratio_r": 5.0, "partial_volume_pct": 50.0,
       "set_runner_tp": True, "breakeven_offset": 0.00005}


def test_view_a_2r_listo_para_parcial():
    vista = exit_policy.management_view(_pos(), {}, CFG, 0.01, 0.01)
    assert vista["r_original"] == pytest.approx(0.00100)
    assert vista["r_actual"] == pytest.approx(2.0)
    assert vista["partial_ready"] is True
    assert vista["can_apply"] is True
    assert vista["partial_volume"] == pytest.approx(0.50)
    assert vista["be_level"] == pytest.approx(1.10017)
    assert vista["runner_level"] == pytest.approx(1.10512)


def test_view_por_debajo_de_2r_no_aplica():
    vista = exit_policy.management_view(
        _pos(price_current=1.10112), {}, CFG, 0.01, 0.01)
    assert vista["r_actual"] == pytest.approx(1.0)
    assert vista["partial_ready"] is False
    assert vista["can_apply"] is False
    assert any("1:2" in m or "2" in m for m in vista["reasons"])


def test_view_con_estado_no_vuelve_a_tomar_el_parcial():
    estado = {"r_original": 0.00100, "partial_taken": 1, "be_applied": 1}
    vista = exit_policy.management_view(_pos(volume=0.50), estado, CFG, 0.01, 0.01)
    assert vista["partial_taken"] is True
    assert vista["partial_ready"] is True
    assert vista["can_apply"] is True
    assert any("ya se tomó" in m for m in vista["reasons"])


def test_view_posicion_pequena_explica_el_motivo():
    """0.01 lotes: el parcial no cabe, pero el BE sí es aplicable."""
    vista = exit_policy.management_view(_pos(volume=0.01), {}, CFG, 0.01, 0.01)
    assert vista["partial_volume"] is None
    assert vista["partial_reason"]
    assert vista["can_apply"] is True


def test_view_r_original_del_estado_no_del_sl_actual():
    """Tras el BE, el SL da ~0 de R: el estado es la única fuente fiable."""
    estado = {"r_original": 0.00100}
    vista = exit_policy.management_view(
        _pos(sl=1.10017, price_current=1.10412), estado, CFG, 0.01, 0.01)
    assert vista["r_original"] == pytest.approx(0.00100)
    assert vista["r_actual"] == pytest.approx(4.0)


def test_view_sin_r_original_no_aplica():
    """SL ya en el precio de entrada (sin offset) y sin estado: no hay base
    de medición, y la vista lo dice en vez de inventar un R."""
    vista = exit_policy.management_view(_pos(sl=1.10012), {}, CFG, 0.01, 0.01)
    assert vista["r_original"] is None
    assert vista["can_apply"] is False
    assert any("R original" in m for m in vista["reasons"])


def test_view_set_runner_tp_false():
    cfg = dict(CFG, set_runner_tp=False)
    vista = exit_policy.management_view(_pos(), {}, cfg, 0.01, 0.01)
    assert vista["set_runner_tp"] is False
    assert vista["runner_level"] is not None  # el nivel se calcula igual; quién decide es el caller


def test_view_runner_ready():
    vista = exit_policy.management_view(
        _pos(price_current=1.10512), {}, CFG, 0.01, 0.01)
    assert vista["runner_ready"] is True
