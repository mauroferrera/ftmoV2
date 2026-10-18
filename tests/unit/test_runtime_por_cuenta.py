"""Tests del enrutado por cuenta (`api.deps.runtime_para_cuenta`).

Con varias cuentas, cada petición puede pedir la suya con `?account=<login>`. La
pieza que lo resuelve es `runtime_para_cuenta`, y lo que se fija aquí es su
contrato:

1. **Sin cuenta, no se construye nada.** La petición por defecto devuelve el
   MISMO runtime que la recibió (`is`), no una copia. Construir un `Runtime` por
   petición para la cuenta activa sería pagar composición en cada lectura.
2. **Con cuenta, se construye el runtime de esa cuenta**, compartiendo el store
   (el estado FTMO es por login, no por proceso) y atando mercado y ejecución al
   login pedido.
3. **Construirlo no conecta.** El adaptador MT5 es perezoso: pedir el runtime de
   una cuenta no abre la terminal.
"""

from __future__ import annotations

from api import deps
from api.runtime import Runtime


class FakeStore:
    """Store mínimo: lo que `Runtime.build` consulta de forma inmediata."""

    def list_db_tables(self):
        return []

    def get_trading_config(self):
        return {"symbols": [], "execution": {}}

    def get_prop_profiles(self):
        return None

    def get_setting(self, key, default=None, table="agent_settings"):
        return default

    def set_setting(self, key, value, table="agent_settings"):
        return None


class FakeMarket:
    def account_info(self):
        return {"login": 1}


def _runtime():
    return Runtime(store=FakeStore(), market=FakeMarket())


class TestRuntimePorCuenta:
    def test_sin_cuenta_devuelve_el_mismo_runtime(self):
        rt = _runtime()
        assert deps.runtime_para_cuenta(rt, None) is rt

    def test_cuenta_vacia_es_la_cuenta_activa(self):
        """`?account=` vacío no debe construir un runtime nuevo."""
        rt = _runtime()
        assert deps.runtime_para_cuenta(rt, "") is rt

    def test_con_cuenta_construye_el_runtime_de_ese_login(self):
        rt = _runtime()
        otro = deps.runtime_para_cuenta(rt, "112125897")
        assert otro is not rt
        assert otro.account_id == "112125897"
        # El store es el MISMO: el estado FTMO vive por login dentro de la misma base.
        assert otro.store is rt.store
        # Mercado y ejecución quedan atados a la cuenta pedida.
        assert otro.market.account_id == "112125897"
        assert otro.execution.account_id == "112125897"

    def test_construir_no_abre_la_terminal(self):
        """El adaptador es perezoso: pedir el runtime no conecta MT5."""
        otro = deps.runtime_para_cuenta(_runtime(), "112125897")
        assert otro.market.session.is_open is False

    def test_dos_cuentas_comparten_la_misma_sesion(self):
        """Una terminal por proceso: los runtimes de cuenta comparten la sesión.

        La cuenta no se elige al crear la sesión (una segunda `initialize()`
        rompería la primera), sino en cada llamada: los adaptadores llevan su
        `account_id` y la sesión cambia de login con `mt5.login()`.
        """
        rt = _runtime()
        a = deps.runtime_para_cuenta(rt, "111")
        b = deps.runtime_para_cuenta(rt, "111")
        c = deps.runtime_para_cuenta(rt, "222")
        assert a.market.session is b.market.session
        assert a.market.session is c.market.session
        # Lo que sí cambia por cuenta es a quién se le pide la lectura.
        assert a.market.account_id == "111"
        assert c.market.account_id == "222"

    def test_el_runtime_real_cachea_uno_por_cuenta(self):
        """El runtime de la app reusa el de cada cuenta (conserva su caché)."""
        rt = _runtime()
        rt._store_backend = None  # como el runtime de `Runtime.build()`
        try:
            a = deps.runtime_para_cuenta(rt, "555")
            b = deps.runtime_para_cuenta(rt, "555")
            c = deps.runtime_para_cuenta(rt, "666")
            assert a is b
            assert a is not c
        finally:
            with deps._RUNTIMES_LOCK:
                deps._RUNTIMES_POR_CUENTA.pop("555", None)
                deps._RUNTIMES_POR_CUENTA.pop("666", None)

    def test_un_runtime_inyectado_no_se_cachea(self):
        """Un runtime de test no deja runtimes en la caché global."""
        rt = _runtime()
        a = deps.runtime_para_cuenta(rt, "777")
        b = deps.runtime_para_cuenta(rt, "777")
        assert a is not b
        assert "777" not in deps._RUNTIMES_POR_CUENTA
