"""`risk_engine.validate_entry` con `force`: qué salta y qué NO.

`force` es un interruptor de OPORTUNIDAD, no de CAPITAL. Estas pruebas fijan la
frontera: salta los juicios de calidad del setup (R:R, invalidez estructural, TTL)
pero NUNCA el presupuesto de riesgo ni la obligación de tener entry/SL. Los
motivos saltados viajan en `force_skipped` para que la auditoría los guarde.
"""

from __future__ import annotations

import pytest

from core import risk_engine


CFG = {"min_rr": 2.0, "setup_ttl_minutes": 45.0}


class TestForceSaltaLoDeOportunidad:
    def test_sin_force_un_rr_bajo_rechaza(self):
        v = risk_engine.validate_entry(
            entry=1.10000, sl=1.09900, target=1.10050, direction="BUY", cfg=CFG)
        assert v["approved"] is False
        assert any("R:R" in r for r in v["reasons"])

    def test_con_force_el_rr_bajo_pasa_a_force_skipped(self):
        v = risk_engine.validate_entry(
            entry=1.10000, sl=1.09900, target=1.10050, direction="BUY",
            cfg=CFG, force=True)
        assert v["approved"] is True
        assert v["reasons"] == []
        assert len(v["force_skipped"]) == 1
        assert "R:R" in v["force_skipped"][0]

    def test_sin_force_la_estructura_rota_rechaza(self):
        v = risk_engine.validate_entry(
            entry=1.10000, sl=1.09900, target=1.10200, direction="BUY",
            current_price=1.09700, invalidate_level=1.09800, cfg=CFG)
        assert v["approved"] is False
        assert any("Estructura rota" in r for r in v["reasons"])

    def test_con_force_la_estructura_rota_no_bloquea(self):
        v = risk_engine.validate_entry(
            entry=1.10000, sl=1.09900, target=1.10200, direction="BUY",
            current_price=1.09700, invalidate_level=1.09800, cfg=CFG, force=True)
        assert v["approved"] is True
        assert any("Estructura rota" in m for m in v["force_skipped"])

    def test_con_force_el_ttl_expirado_no_bloquea(self):
        v = risk_engine.validate_entry(
            entry=1.10000, sl=1.09900, target=1.10200, direction="BUY",
            created_at=1000.0, now=1000.0 + 60 * 60, cfg=CFG, force=True)
        assert v["approved"] is True
        assert any("expirado" in m.lower() for m in v["force_skipped"])


class TestForceNoSaltaLoDeCapital:
    def test_el_presupuesto_de_riesgo_sigue_mandando(self):
        """Un setup perfecto forzado NO puede saltarse la cabeza de riesgo."""
        v = risk_engine.validate_entry(
            entry=1.10000, sl=1.09900, target=1.10200, direction="BUY",
            cfg=CFG, force=True,
            balance=1000.0, risk_pct=1.0, loss_per_lot=50.0, lot=1.0)
        assert v["approved"] is False
        assert any("presupuesto" in r.lower() for r in v["reasons"])

    def test_sin_entry_o_sl_sigue_rechazando(self):
        v = risk_engine.validate_entry(
            entry=1.10000, sl=None, target=None, direction="BUY",
            cfg=CFG, force=True)
        assert v["approved"] is False
        assert any("entry/SL" in r for r in v["reasons"])

    def test_force_no_marca_nada_en_un_setup_limpio(self):
        v = risk_engine.validate_entry(
            entry=1.10000, sl=1.09900, target=1.10200, direction="BUY",
            cfg=CFG, force=True)
        assert v["approved"] is True
        assert v["force_skipped"] == []


def test_sin_force_force_skipped_esta_vacio_y_la_clave_existe():
    v = risk_engine.validate_entry(
        entry=1.10000, sl=1.09900, target=1.10200, direction="BUY", cfg=CFG)
    assert v["approved"] is True
    assert v["force_skipped"] == []


@pytest.mark.parametrize("direction,precio,nivel", [
    ("BUY", 1.09700, 1.09800),
    ("SELL", 1.10300, 1.10200),
])
def test_force_cubre_las_dos_direcciones(direction, precio, nivel):
    v = risk_engine.validate_entry(
        entry=1.10000, sl=1.09900, target=1.10200, direction=direction,
        current_price=precio, invalidate_level=nivel, cfg=CFG, force=True)
    assert v["approved"] is True
    assert v["force_skipped"]
