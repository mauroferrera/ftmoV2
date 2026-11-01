"""Las rutas de ejecución: lo único que toca dinero, y las puertas que lo rodean.

Tres escrituras, una lectura y la gestión parcial
-----------------------------------------------
`POST /api/trade/market` abre una posición. `POST /api/positions/{ticket}/close`
la cierra. `POST /api/positions/{ticket}/modify` mueve su stop (y/o su objetivo).
`GET /api/positions` mira lo que hay abierto. `GET
/api/positions/{ticket}/management` consulta la gestión parcial escalada y
`POST` la aplica (cierra el primer tramo a 1:2R, mueve el SL del resto a
break-even + offset y deja correr el runner hacia 1:5R).

**Las escrituras piden token; las lecturas no.** No es una comodidad: abrir,
cerrar, mover un stop o aplicar una gestión mueven dinero, y una ruta sin token
convierte cualquier página web en un cliente de la API con la sesión del usuario.
Mirar posiciones o el plan de gestión no cambia nada, así que exigir token ahí
solo impediría pintar el panel.

Dónde vive la lógica y por qué no aquí
--------------------------------------
Ni el dimensionado, ni el gate FTMO, ni la calidad de ejecución viven en este
fichero: los tres son `ExecutionService` (y, debajo, `core/`). Esta capa hace dos
cosas y ninguna más:

1. Elige el servicio del runtime (con su puerto de ejecución inyectable).
2. Traduce el `(cuerpo, status)` que devuelve el servicio a HTTP.

**Por qué el status lo decide el servicio.** El servicio es el MISMO que usa
cualquier camino automático (el watcher, el arranque). Si la ruta validara por su
cuenta, el camino con menos puertas acabaría siendo el automático, que es
exactamente el que menos gente está mirando cuando manda la orden.

Por qué `HTTPException(status, detail=cuerpo)` y no un `JSONResponse`
--------------------------------------------------------------------
El cuerpo del servicio ya trae su propio `status` en el JSON (`"SYMBOL_NOT_ALLOWED"`,
`"LOT_UNAVAILABLE"`, …) y el código HTTP va aparte. `HTTPException` deja el cuerpo
intacto dentro de `detail` y Starlette le pone el código; construir a mano un
`JSONResponse` en cada handler duplicaría el mapa de códigos que el servicio ya
resolvió. El `detail` con un dict es la forma en que el proyecto ya responde 501
en la ruta de auto-ejecución del watcher, así que el frontend lee lo mismo.

Las lecturas (`GET /api/positions`)
-----------------------------------
No piden token, no escriben auditoría y no mandan nada. El servicio devuelve
`(filas, error)`: `error` distingue "no hay puerto de ejecución cableado" de "el
puerto no sabe listar", y aquí solo se traduce a 503 con el motivo. Una lista
vacía NO es un error: es "no hay posiciones".
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import deps
from ..services.execution import DEFAULT_TIMEFRAME, execution_from
from ..services.replication import replicar_orden

router = APIRouter(tags=["execution"])


class TradeMarketBody(BaseModel):
    """Orden de mercado manual (el botón de "operar" del panel).

    `volume` es opcional a propósito: si no viene, el lote se dimensiona por el
    riesgo efectivo del setup, con la MISMA cuenta que usa el resto del sistema. El
    `score` y el `verdict` que llegan aquí son el contexto con el que el panel
    decidió enseñar el botón: se pasan al servicio para que el riesgo efectivo sea
    el mismo que la pantalla mostró, y no uno recalculado en otro sitio.

    `no_tp=True` deja la orden sin objetivo cuando la salida la decide una política
    y no un target congelado. `invalidate_level` y `planned_entry` alimentan la
    comprobación de calidad de ejecución (deriva e invalidación), no el envío.

    `force=True` salta las puertas restrictivas de OPORTUNIDAD (lista blanca de
    símbolos, R:R/invalidez/TTL y calidad de ejecución) para poder operar fuera de
    killzone o cuando la oportunidad no encaja con la estrategia. NO salta los
    topes de capital: los cortes FTMO, el SL obligatorio, el lote y el presupuesto
    de riesgo siguen mandando, y la decisión forzada queda auditada (`forced=1`).

    `replicate=True` repite la MISMA orden en todas las cuentas de `prop_profiles`
    (la maestra primero), cada una con sus propias puertas. El cuerpo de la
    respuesta cambia de forma: en vez de una orden, lleva `accounts` con el
    resultado por login y un `summary`. Ver `api/services/replication.py`.
    """

    symbol: str
    action: str = "BUY"
    volume: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    sl_distance: Optional[float] = None
    tp_distance: Optional[float] = None
    no_tp: bool = False
    force: bool = False
    replicate: bool = False
    magic: Optional[int] = None
    comment: Optional[str] = None
    deviation: Optional[int] = None
    score: Optional[float] = None
    verdict: Optional[str] = None
    invalidate_level: Optional[float] = None
    planned_entry: Optional[float] = None
    created_at: Optional[float] = None
    timeframe: Optional[str] = None


class ModifyStopBody(BaseModel):
    """Lo que la ruta de modificación manda: los niveles nuevos, por separado.

    `sl` y `tp` se aceptan independientes porque un trailing mueve el stop y deja
    el objetivo quieto. `None` significa "no tocar ese nivel": el bróker conserva
    el que ya tiene. Quitar un nivel es `0.0`, que es la forma en que MT5 lo
    entiende — y por eso el servicio rechaza el caso en que los dos sean `None`
    (no habría nada que modificar) sin confundirlo con "quitar los dos".
    """

    sl: Optional[float] = None
    tp: Optional[float] = None


def _servicio(rt: Any, account: Optional[str] = None) -> Any:
    """El `ExecutionService` para esta petición, con los puertos del runtime.

    Se construye por petición y NO se cachea: a diferencia del watcher, el servicio
    de ejecución no guarda memoria de ciclo, y sus colaboradores (el puerto MT5, el
    `PropService`, el store) viven en el `Runtime`. Construirlo aquí es lo que
    permite inyectar un puerto de ejecución en un test sin tocar la ruta.
    """
    return execution_from(deps.runtime_para_cuenta(rt, account))


@router.post("/api/trade/market", response_model=None)
def post_trade_market(
    body: TradeMarketBody,
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
    account: Optional[str] = Query(
        None, description="login MT5 de la cuenta (varias cuentas)"),
) -> Dict[str, Any]:
    """Abre una posición a mercado, pasando por TODAS las puertas.

    El servicio decide el status (403 si la lista blanca o el FTMO bloquean, 503 si
    no hay mercado o puerto, 400 si el lote o la geometría no valen, 200 si sale).
    Aquí solo se traduce: `status >= 400` se levanta como excepción y el cuerpo del
    servicio viaja íntegro en `detail`.

    `source="manual"` marca la fila de auditoría: la MISMA orden puede llegar desde
    el botón del panel o desde un escaneo automático, y la fila tiene que decir por
    cuál de los dos caminos se pidió.

    `account` selecciona la cuenta contra la que se evalúa y se envía la orden
    (varias cuentas); sin ella, la del runtime que recibió la petición.

    Con `replicate=True` la orden sale en TODAS las cuentas configuradas: la
    maestra primero (la de `account`, o la activa) y después el resto, cada una
    con su propio `ExecutionService` y sus propias puertas. Una cuenta que falla
    no cancela las demás; el cuerpo dice cómo fue en cada una y solo es 4xx/5xx
    si fallaron todas.
    """
    orden = dict(
        symbol=body.symbol, action=body.action, volume=body.volume,
        sl=body.sl, tp=body.tp,
        sl_distance=body.sl_distance, tp_distance=body.tp_distance, no_tp=body.no_tp,
        magic=body.magic, comment=body.comment, deviation=body.deviation,
        score=body.score, verdict=body.verdict, invalidate_level=body.invalidate_level,
        planned_entry=body.planned_entry, created_at=body.created_at,
        timeframe=body.timeframe or DEFAULT_TIMEFRAME, source="manual",
        force=body.force,
    )
    if body.replicate:
        cuerpo, status = replicar_orden(
            rt,
            account or getattr(rt, "account_id", None),
            orden,
            deps.runtime_para_cuenta,
        )
    else:
        cuerpo, status = _servicio(rt, account).execute_market_trade(**orden)
    if status >= 400:
        raise HTTPException(status_code=status, detail=cuerpo)
    return cuerpo


@router.get("/api/positions", response_model=None)
def get_positions(
    magic: Optional[int] = None,
    rt: Any = Depends(deps.runtime),
    account: Optional[str] = Query(
        None, description="login MT5 de la cuenta (varias cuentas)"),
) -> Dict[str, Any]:
    """Las posiciones abiertas, filtradas por `magic` si se pasa.

    Lectura: sin token, sin auditoría, sin envío. `magic` filtra en la ruta (no en
    la ruta HTTP, en el servicio) para que el panel pueda enseñar solo las
    operaciones del sistema y no las que el operador tenga abiertas a mano.

    Un `error` del servicio es un 503: significa que no se puede contestar (sin
    puerto de ejecución, o un puerto que se calló). Una lista vacía es un 200: "no
    hay posiciones" es una respuesta, no un fallo.
    """
    filas, error = _servicio(rt, account).positions(magic=magic, with_management=True)
    if error:
        raise HTTPException(status_code=503, detail={"error": error})
    return {"positions": list(filas or [])}


@router.post("/api/positions/{ticket}/close", response_model=None)
def post_close_position(
    ticket: int,
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
    account: Optional[str] = Query(
        None, description="login MT5 de la cuenta (varias cuentas)"),
) -> Dict[str, Any]:
    """Cierra la posición indicada por ticket.

    El ticket va en la ruta y no en un cuerpo porque es un identificador: se puede
    enlazar y comprobar sin abrir el cuerpo de la petición.

    El cierre NO pasa por la lista blanca ni por el gate FTMO (lo razona el
    servicio): un tope para ABRIR no puede dejar una posición viva sin forma de
    salir. El 404 cuando no hay posición con ese ticket es el caso normal de un
    doble clic, no un fallo del sistema.
    """
    cuerpo, status = _servicio(rt, account).close_position(ticket=ticket)
    if status >= 400:
        raise HTTPException(status_code=status, detail=cuerpo)
    return cuerpo


@router.post("/api/positions/{ticket}/modify", response_model=None)
def post_modify_position(
    ticket: int,
    body: ModifyStopBody,
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
    account: Optional[str] = Query(
        None, description="login MT5 de la cuenta (varias cuentas)"),
) -> Dict[str, Any]:
    """Mueve el SL/TP de la posición abierta `ticket`.

    Mover un stop no abre riesgo (lo cierra o lo deja igual), así que —igual que el
    cierre— no pasa por las puertas de apertura. La garantía de que el stop no se
    afloja está en el cálculo de salida que decide el nivel (el ratchet "nunca
    afloja"), no aquí: esta ruta manda el nivel que le llega, tal cual.

    Un cuerpo con `sl` y `tp` a `None` es un 400 del servicio ("no hay nada que
    modificar"), que es distinto de `0.0` ("quita el nivel").
    """
    cuerpo, status = _servicio(rt, account).modify_stop(ticket, sl=body.sl, tp=body.tp)
    if status >= 400:
        raise HTTPException(status_code=status, detail=cuerpo)
    return cuerpo


class ManagementBody(BaseModel):
    """Cuerpo del POST de gestión. Todo opcional: el ticket va en la ruta.

    `force` salta el gate de "¿lleva ya 1:2R?" SOLO para break-even y runner:
    nunca cierra el parcial antes de su ratio. Es la palanca del operador para
    proteger una posición a mano (D-005: la decisión es humana y explícita).
    """

    force: bool = False


@router.get("/api/positions/{ticket}/management", response_model=None)
def get_position_management(
    ticket: int,
    rt: Any = Depends(deps.runtime),
    account: Optional[str] = Query(
        None, description="login MT5 de la cuenta (varias cuentas)"),
) -> Dict[str, Any]:
    """QUÉ se puede aplicar de la gestión parcial a `ticket` (consulta, sin token).

    Lectura pura: no mueve dinero ni escribe estado. Devuelve el R actual, si el
    precio ya alcanzó el ratio del primer tramo, el volumen que se cerraría, el
    nivel de break-even y el del runner. El panel usa esto para habilitar el
    botón; el POST vuelve a calcular todo al vuelo con los números de AHORA.
    """
    cuerpo, status = _servicio(rt, account).management_view(ticket)
    if status >= 400:
        raise HTTPException(status_code=status, detail=cuerpo)
    return cuerpo


@router.post("/api/positions/{ticket}/management", response_model=None)
def post_position_management(
    ticket: int,
    body: Optional[ManagementBody] = None,
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
    account: Optional[str] = Query(
        None, description="login MT5 de la cuenta (varias cuentas)"),
) -> Dict[str, Any]:
    """Aplica la gestión parcial escalada a `ticket`.

    El orden de pasos es la garantía (lo razona el servicio): parcial PRIMERO,
    break-even DESPUÉS, y si el parcial falla el SL no se toca. El token es
    obligatorio: esto cierra dinero y mueve stops (D-005: confirmación humana).

    Un 409 (`not_ready`) no es un fallo del sistema: es la respuesta honesta de
    "aún no llegó a 1:2R", con los motivos en `reasons`, para que el panel
    pueda enseñarlos sin inventar un error.
    """
    force = bool(body.force) if body is not None else False
    cuerpo, status = _servicio(rt, account).apply_management(ticket, force=force)
    if status >= 400:
        raise HTTPException(status_code=status, detail=cuerpo)
    return cuerpo


__all__ = ["ManagementBody", "ModifyStopBody", "TradeMarketBody", "router"]
