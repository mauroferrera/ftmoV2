"""Réplica de una orden en todas las cuentas configuradas.

Por qué existe
--------------
El paquete MT5 solo mantiene una terminal conectada por proceso, así que el
dashboard no puede tener las cuatro cuentas "vivas" a la vez. Lo que sí puede
hacer es, con UNA terminal, cambiar de cuenta (`mt5.login()`) y repetir la misma
orden en cada una. Eso es exactamente lo que pide el operador: pulsar BUY/SELL
una vez y que la orden salga en todas.

Qué NO hace
-----------
- **No comparte puertas.** Cada cuenta pasa por su propio `ExecutionService`, con
  sus topes FTMO, su SL obligatorio y su dimensionado. Si una cuenta está
  bloqueada, esa cuenta falla y las demás siguen; el resultado lo dice cuenta a
  cuenta. Nunca se "fuerza" una cuenta porque otra haya aceptado.
- **No inventa cuentas.** La lista sale de `prop_profiles` (`accounts:`), que es
  la misma que ve el selector del dashboard. Sin cuentas, es un 400 visible.
- **No oculta fallos.** El cuerpo lleva `accounts` con el status y el error de
  cada login. La UI los enseña todos, no solo el primero.
- **Deja la terminal en la maestra.** Al terminar vuelve a la cuenta activa: es
  la que el operador mira en la ventana de MT5 y la que pedirá la siguiente
  lectura del panel, que si no pagaría un cambio de login.

Lote
----
Si la orden trae `volume`, ese lote se usa en todas. Si viene vacío (lote
automático), se ejecuta primero la cuenta maestra y el lote que el bróker aceptó
se pasa como explícito a las demás: así la réplica es el MISMO lote, no cuatro
dimensionados que pueden diferir por perfil o por redondeo.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from .execution import execution_from


def cuentas_configuradas(rt: Any) -> List[str]:
    """Los logins mapeados en `prop_profiles` (los mismos del selector de UI)."""
    store = getattr(rt, "store", None)
    if store is None:
        return []
    try:
        perfiles = store.get_prop_profiles()
    except Exception:  # noqa: BLE001 - sin perfiles no hay réplica, y se dice
        return []
    if perfiles is None:
        return []
    try:
        return [str(c) for c in perfiles.account_ids()]
    except Exception:  # noqa: BLE001 - un registro raro no debe tumbar la ruta
        return []


def replicar_orden(
    rt: Any,
    master: Optional[str],
    orden: Dict[str, Any],
    resolver: Callable[[Any, Optional[str]], Any],
    cuentas: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], int]:
    """Ejecuta `orden` en la maestra y en el resto de cuentas, en orden.

    `resolver` es `deps.runtime_para_cuenta`: se inyecta para que el servicio no
    dependa de la capa HTTP y para poder probarlo con dobles. Devuelve
    `(cuerpo, status)` como el resto de servicios: 200 si AL MENOS una cuenta
    aceptó; si todas fallaron, el status de la primera que falló.
    """
    lista = [str(c) for c in (cuentas if cuentas is not None else cuentas_configuradas(rt))]
    maestra = str(master) if master else (lista[0] if lista else None)
    # La maestra SIEMPRE participa, aunque `cuentas` la haya recortado: es la
    # cuenta en la que el operador pulsó el botón.
    if maestra:
        lista = [maestra] + [c for c in lista if c != maestra]
    if not lista:
        return ({"error": "No hay cuentas configuradas para replicar. Añade los "
                           "logins en prop_profiles.yaml (accounts:) o en "
                           "MT5_ACCOUNTS.",
                 "status": "NO_ACCOUNTS"}, 400)

    lote = orden.get("volume")
    resultados: List[Dict[str, Any]] = []
    runtime_maestra: Any = None

    for indice, login in enumerate(lista):
        kwargs = dict(orden)
        # A partir de la segunda cuenta, el lote que aceptó la maestra manda.
        if indice > 0 and kwargs.get("volume") is None and lote is not None:
            kwargs["volume"] = lote
        try:
            destino = resolver(rt, login)
            if indice == 0:
                runtime_maestra = destino
            servicio = execution_from(destino)
            cuerpo, status = servicio.execute_market_trade(**kwargs)
        except Exception as exc:  # noqa: BLE001 - una cuenta no tumba la réplica
            cuerpo, status = ({"error": "No se pudo replicar en la cuenta {0}: {1}"
                                        .format(login, exc),
                               "status": "REPLICATION_FAILED"}, 503)
        # `http` va aparte del `status` del servicio (que es su etiqueta de
        # negocio): mezclarlos haría que el JSON de una cuenta tapara el código.
        registro: Dict[str, Any] = {"account": login, "http": int(status)}
        registro.update(cuerpo or {})
        registro["ok"] = bool((cuerpo or {}).get("ok"))
        resultados.append(registro)
        if indice == 0 and lote is None:
            lote = (cuerpo or {}).get("volume") or (cuerpo or {}).get("requested_volume")

    _volver_a_la_maestra(runtime_maestra, maestra)

    exitos = [r for r in resultados if r.get("ok")]
    salida: Dict[str, Any] = {
        "ok": bool(exitos),
        "replicated": True,
        "master": maestra,
        "accounts": resultados,
        "summary": {
            "total": len(resultados),
            "ok": len(exitos),
            "failed": len(resultados) - len(exitos),
        },
    }
    if not exitos:
        primero = next((r for r in resultados if r["http"] >= 400), resultados[0])
        return salida, int(primero.get("http") or 400)
    return salida, 200


def _volver_a_la_maestra(runtime_maestra: Any, maestra: Optional[str]) -> None:
    """Deja la terminal en la cuenta maestra después de replicar.

    Dos motivos, los dos visibles para el operador: (1) la ventana de MT5 se
    quedaría mostrando la ÚLTIMA cuenta de la lista, que no es la que está
    mirando; (2) la siguiente lectura del panel pediría la maestra y pagaría un
    cambio de login (~1-2 s) por algo que ya sabíamos. Nunca lanza: las órdenes
    ya salieron y fallar aquí no cambia el resultado de la réplica.
    """
    if not maestra or runtime_maestra is None:
        return
    sesion = getattr(getattr(runtime_maestra, "market", None), "session", None)
    if sesion is None or not hasattr(sesion, "account"):
        return
    try:
        with sesion.account(maestra):
            pass
    except Exception:  # noqa: BLE001 - restaurar es cortesía, no parte de la orden
        return


__all__ = ["cuentas_configuradas", "replicar_orden"]
