"""Consistencia del `strategy.yaml` de fábrica con la lista blanca de ejecución.

Estos tests leen el YAML REAL (solo lectura). A diferencia de los dobles de
config, su valor es detectar en frío lo que en caliente solo se ve al confirmar
una orden: un símbolo operable que la estrategia no analiza, uno sin distancia de
SL (el gate lo rechazaría) o un par JPY con un offset de break-even pensado para
5 dígitos. D-026 amplió la lista blanca a los 12 símbolos analizados.
"""

from __future__ import annotations

import yaml

from core import paths

#: Pares con 3 dígitos: medio pip son 0.005, no 0.00005.
JPY_PAIRS = {"USDJPY", "EURJPY", "AUDJPY"}


def _cfg() -> dict:
    with open(paths.STRATEGY_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _execution(cfg: dict) -> dict:
    return cfg.get("execution") or {}


def _allow(cfg: dict) -> list:
    return [str(s).strip().upper()
            for s in (_execution(cfg).get("symbols_allow") or [])]


def test_cada_simbolo_operable_esta_en_los_analizados():
    cfg = _cfg()
    analizados = {
        str(e.get("symbol") or "").strip().upper()
        for e in (cfg.get("symbols") or []) if isinstance(e, dict)
    }
    allow = set(_allow(cfg))
    assert allow, "symbols_allow vacío: se permitiría operar cualquier símbolo"
    fuera = sorted(allow - analizados)
    assert not fuera, (
        "operables fuera de la lista que analiza la estrategia: {0}".format(fuera))


def test_cada_simbolo_operable_tiene_distancia_de_sl_positiva():
    cfg = _cfg()
    distancias = _execution(cfg).get("sl_distance_by_symbol") or {}
    faltan = [
        s for s in _allow(cfg)
        if isinstance(distancias.get(s), bool)
        or not isinstance(distancias.get(s), (int, float))
        or distancias.get(s) <= 0
    ]
    assert not faltan, (
        "symbols_allow sin sl_distance_by_symbol > 0: {0}".format(faltan))


def test_pares_jpy_operables_tienen_offset_de_break_even():
    cfg = _cfg()
    offsets = _execution(cfg).get("breakeven_offset_by_symbol") or {}
    faltan = sorted(s for s in (set(_allow(cfg)) & JPY_PAIRS) if s not in offsets)
    assert not faltan, (
        "pares JPY sin breakeven_offset_by_symbol (el global es ~0 en 3 "
        "dígitos): {0}".format(faltan))
