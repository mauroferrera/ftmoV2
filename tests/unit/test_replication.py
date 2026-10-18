"""Tests de la réplica: una orden, N cuentas, N puertas.

Lo que se fija aquí es el contrato de `replicar_orden` y no el bróker:

1. **La maestra primero.** El orden de ejecución no es el del YAML: la cuenta
   activa manda, y las demás la siguen. Si la maestra falla, las demás siguen.
2. **Mismo lote.** Con lote automático, el que aceptó el bróker en la maestra se
   pasa EXPLÍCITO al resto; no se deja que cada cuenta dimensione el suyo.
3. **Una cuenta no tumba la réplica.** El fallo (o la excepción) de una se
   reporta en su fila y el bucle continúa.
4. **El `http` no se confunde con el `status` del servicio.** El segundo es una
   etiqueta de negocio (`rejected`, `BLOCKED_BY_RISK`, …) y el primero es el
   código con el que se responde.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Dict, List, Optional

from api.services import replication


class PerfilesFalsos:
    def __init__(self, logins: List[str]) -> None:
        self._logins = list(logins)

    def account_ids(self) -> List[str]:
        return list(self._logins)


class StoreFalso:
    def __init__(self, logins: Optional[List[str]] = None,
                 perfiles: Any = "auto") -> None:
        self._perfiles = (PerfilesFalsos(logins or []) if perfiles == "auto"
                          else perfiles)

    def get_prop_profiles(self):
        if isinstance(self._perfiles, Exception):
            raise self._perfiles
        return self._perfiles


class RuntimeFalso:
    def __init__(self, store: Any) -> None:
        self.store = store


class ServicioFalso:
    """El `ExecutionService` de una cuenta, sin puertas ni bróker."""

    def __init__(self, cuerpo: Optional[Dict[str, Any]] = None,
                 status: int = 200, revienta: Optional[BaseException] = None) -> None:
        self.cuerpo = cuerpo if cuerpo is not None else {
            "ok": True, "volume": 0.10, "status": None}
        self.status = status
        self.revienta = revienta
        self.llamadas: List[Dict[str, Any]] = []

    def execute_market_trade(self, **kwargs):
        self.llamadas.append(dict(kwargs))
        if self.revienta is not None:
            raise self.revienta
        return dict(self.cuerpo), self.status


def _resolver(servicios: Dict[str, ServicioFalso]):
    """El `runtime_para_cuenta` de mentira: cada login con SU servicio."""
    pedidos: List[Any] = []

    def resolver(rt, account):
        pedidos.append(account)
        return servicios[account]

    resolver.pedidos = pedidos  # type: ignore[attr-defined]
    return resolver


def _parchea_servicios(monkeypatch, servicios: Dict[str, ServicioFalso]):
    monkeypatch.setattr(replication, "execution_from",
                        lambda rt: rt)  # el "runtime" ES el servicio falso
    return _resolver(servicios)


ORDEN = {"symbol": "EURUSD", "action": "BUY", "volume": 0.10,
         "sl_distance": 0.001, "tp_distance": 0.002}


class TestCuentasConfiguradas:
    def test_salen_los_logins_del_perfil(self):
        rt = RuntimeFalso(StoreFalso(["111", "222"]))
        assert replication.cuentas_configuradas(rt) == ["111", "222"]

    def test_sin_store_no_hay_cuentas(self):
        assert replication.cuentas_configuradas(RuntimeFalso(None)) == []

    def test_sin_perfiles_no_hay_cuentas(self):
        assert replication.cuentas_configuradas(RuntimeFalso(StoreFalso(perfiles=None))) == []

    def test_un_store_que_revienta_no_tumba_la_ruta(self):
        rt = RuntimeFalso(StoreFalso(perfiles=RuntimeError("sin base")))
        assert replication.cuentas_configuradas(rt) == []


class TestReplicarOrden:
    def test_sin_cuentas_es_400_visible(self, monkeypatch):
        _parchea_servicios(monkeypatch, {})
        cuerpo, status = replication.replicar_orden(
            RuntimeFalso(StoreFalso([])), None, dict(ORDEN), lambda rt, c: rt)
        assert status == 400
        assert cuerpo["status"] == "NO_ACCOUNTS"

    def test_la_maestra_va_primero_y_el_resto_la_sigue(self, monkeypatch):
        servicios = {login: ServicioFalso() for login in ("111", "222", "333")}
        resolver = _parchea_servicios(monkeypatch, servicios)
        rt = RuntimeFalso(StoreFalso(["111", "222", "333"]))

        cuerpo, status = replication.replicar_orden(
            rt, "333", dict(ORDEN), resolver)

        assert status == 200
        assert cuerpo["master"] == "333"
        assert [a["account"] for a in cuerpo["accounts"]] == ["333", "111", "222"]
        assert resolver.pedidos == ["333", "111", "222"]
        assert cuerpo["summary"] == {"total": 3, "ok": 3, "failed": 0}

    def test_el_lote_aceptado_por_la_maestra_manda_en_las_demas(self, monkeypatch):
        """Lote automático: el bróker de la maestra aceptó 0.37 y ese es el lote."""
        servicios = {
            "111": ServicioFalso({"ok": True, "volume": 0.37, "status": None}),
            "222": ServicioFalso({"ok": True, "volume": 0.37, "status": None}),
        }
        resolver = _parchea_servicios(monkeypatch, servicios)
        orden = dict(ORDEN, volume=None)

        replication.replicar_orden(
            RuntimeFalso(StoreFalso(["111", "222"])), "111", orden, resolver)

        assert servicios["111"].llamadas[0]["volume"] is None
        assert servicios["222"].llamadas[0]["volume"] == 0.37

    def test_el_lote_explicito_se_respeta_en_todas(self, monkeypatch):
        servicios = {login: ServicioFalso() for login in ("111", "222")}
        resolver = _parchea_servicios(monkeypatch, servicios)

        replication.replicar_orden(
            RuntimeFalso(StoreFalso(["111", "222"])), "111", dict(ORDEN), resolver)

        assert servicios["222"].llamadas[0]["volume"] == 0.10

    def test_una_cuenta_que_falla_no_cancela_las_demas(self, monkeypatch):
        servicios = {
            "111": ServicioFalso({"ok": True, "volume": 0.10}),
            "222": ServicioFalso({"ok": False, "status": "rejected",
                                  "error": "sin margen"}, status=400),
            "333": ServicioFalso({"ok": True, "volume": 0.10}),
        }
        resolver = _parchea_servicios(monkeypatch, servicios)

        cuerpo, status = replication.replicar_orden(
            RuntimeFalso(StoreFalso(["111", "222", "333"])), "111", dict(ORDEN),
            resolver)

        assert status == 200  # al menos una salió
        assert cuerpo["ok"] is True
        assert cuerpo["summary"] == {"total": 3, "ok": 2, "failed": 1}
        filas = {a["account"]: a for a in cuerpo["accounts"]}
        assert filas["222"]["ok"] is False
        assert filas["222"]["http"] == 400
        assert filas["222"]["status"] == "rejected"  # la etiqueta NO se pisa
        assert servicios["333"].llamadas, "el bucle siguió después del fallo"

    def test_una_excepcion_se_reporta_y_el_bucle_continua(self, monkeypatch):
        servicios = {
            "111": ServicioFalso({"ok": True, "volume": 0.10}),
            "222": ServicioFalso(revienta=RuntimeError("puerto roto")),
        }
        resolver = _parchea_servicios(monkeypatch, servicios)

        cuerpo, status = replication.replicar_orden(
            RuntimeFalso(StoreFalso(["111", "222"])), "111", dict(ORDEN), resolver)

        assert status == 200
        fila = next(a for a in cuerpo["accounts"] if a["account"] == "222")
        assert fila["http"] == 503
        assert "puerto roto" in fila["error"]

    def test_si_fallan_todas_el_status_es_el_de_la_primera(self, monkeypatch):
        cuerpo_rechazo = {"ok": False, "status": "NO_EXECUTION_PORT"}
        servicios = {
            "111": ServicioFalso(dict(cuerpo_rechazo), status=503),
            "222": ServicioFalso(dict(cuerpo_rechazo), status=503),
        }
        resolver = _parchea_servicios(monkeypatch, servicios)

        cuerpo, status = replication.replicar_orden(
            RuntimeFalso(StoreFalso(["111", "222"])), "111", dict(ORDEN), resolver)

        assert status == 503
        assert cuerpo["ok"] is False
        assert cuerpo["summary"] == {"total": 2, "ok": 0, "failed": 2}

    def test_la_lista_de_cuentas_se_puede_forzar(self, monkeypatch):
        """El parámetro `cuentas` limita la réplica (útil para probar por partes).

        La maestra SIEMPRE participa: es la cuenta en la que el operador pulsó el
        botón, y saltarla sería no ejecutar nada donde miró. `cuentas` solo
        recorta el resto.
        """
        servicios = {login: ServicioFalso() for login in ("111", "222")}
        resolver = _parchea_servicios(monkeypatch, servicios)

        cuerpo, status = replication.replicar_orden(
            RuntimeFalso(StoreFalso(["111", "222"])), "111", dict(ORDEN), resolver,
            cuentas=["222"])

        assert status == 200
        assert [a["account"] for a in cuerpo["accounts"]] == ["111", "222"]
        assert resolver.pedidos == ["111", "222"]


class SesionFalsa:
    """La sesión MT5 del runtime, sin terminal: apunta los cambios de cuenta."""

    def __init__(self, revienta: bool = False) -> None:
        self.cuentas: List[str] = []
        self.revienta = revienta

    def account(self, login):
        self.cuentas.append(login)
        if self.revienta:
            raise RuntimeError("terminal caída")
        return nullcontext()


class NodoConSesion:
    """Runtime con su sesión y el servicio que `execution_from` devolvería."""

    def __init__(self, servicio: "ServicioFalso", sesion: SesionFalsa) -> None:
        self._servicio = servicio
        self.market = type("Mercado", (), {"session": sesion})()

    def execute_market_trade(self, **kwargs):
        return self._servicio.execute_market_trade(**kwargs)


class TestVolverALaMaestra:
    """Después de replicar, la terminal queda en la cuenta activa."""

    @staticmethod
    def _resolver(monkeypatch, sesion: SesionFalsa, nodos=None):
        monkeypatch.setattr(replication, "execution_from", lambda rt: rt)
        nodos = nodos or {}

        def resolver(rt, account):
            return nodos.setdefault(account, NodoConSesion(ServicioFalso(), sesion))

        return resolver

    def test_la_ultima_cuenta_no_se_queda_conectada(self, monkeypatch):
        sesion = SesionFalsa()
        resolver = self._resolver(monkeypatch, sesion)

        replication.replicar_orden(
            RuntimeFalso(StoreFalso(["111", "222", "333"])), "111", dict(ORDEN),
            resolver)

        # El último cambio de cuenta pedido es el de la maestra.
        assert sesion.cuentas == ["111"]

    def test_si_restaurar_falla_no_cambia_el_resultado(self, monkeypatch):
        sesion = SesionFalsa(revienta=True)
        resolver = self._resolver(monkeypatch, sesion)

        cuerpo, status = replication.replicar_orden(
            RuntimeFalso(StoreFalso(["111", "222"])), "111", dict(ORDEN), resolver)

        assert status == 200
        assert cuerpo["summary"] == {"total": 2, "ok": 2, "failed": 0}

    def test_sin_sesion_no_intenta_nada(self, monkeypatch):
        """Con dobles sin sesión (la mayoría de tests) es un no-op."""
        servicios = {login: ServicioFalso() for login in ("111", "222")}
        resolver = _parchea_servicios(monkeypatch, servicios)

        cuerpo, status = replication.replicar_orden(
            RuntimeFalso(StoreFalso(["111", "222"])), "111", dict(ORDEN), resolver)

        assert status == 200
        assert cuerpo["ok"] is True
