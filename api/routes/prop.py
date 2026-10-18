"""Rutas de prop firm: el estado del challenge de la cuenta activa.

Endpoints delgados: no calculan nada. La cuenta la lee `MarketService`, el perfil
lo resuelve `PropService` desde `config/prop_profiles.yaml` y la regla FTMO vive
en `core/prop_rules.py`. Aquí solo se elige el servicio del runtime y se devuelve.

Los errores no se traducen aquí: `AdapterError` (terminal caída) y
`ConfigUnavailable` (sin store) son 503; `PropProfilesError` es un `ValueError`
y sale 400 (un YAML de topes mal escrito se ve, no se esconde); los recoge
`api/errors.py` a nivel de app.

Con varias cuentas, cada ruta acepta `?account=<login>`: sin él, la petición se
resuelve con el runtime que la recibió (la cuenta activa del contenedor); con él,
con la cuenta pedida.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from api.services.prop import PropService

from .. import deps

router = APIRouter(prefix="/api/prop", tags=["prop"])


def prop(rt=Depends(deps.runtime)) -> PropService:
    """El servicio de prop del runtime. 503 si no está cableado."""
    return deps.prop_service(rt)


@router.get("/state", response_model=None)
def state(
    rt: Any = Depends(deps.runtime),
    account: Optional[str] = Query(
        None, description="login MT5 de la cuenta (varias cuentas)"),
) -> Dict[str, Any]:
    """El veredicto del shell prop para la cuenta pedida.

    Siempre la misma forma. Con la cuenta sin perfil mapeado devuelve
    `tracked=false` y el motivo (`fail-closed`): es un HECHO que la UI muestra, no
    un error. El 404/503/400 solo aparece cuando falla el bróker o la config.

    Sin `account` responde con la del runtime (la cuenta activa); con `?account=`,
    con la que se pide explícitamente.
    """
    svc = deps.prop_service(deps.runtime_para_cuenta(rt, account))
    return svc.state()


@router.get("/profiles", response_model=None)
def profiles(
    rt: Any = Depends(deps.runtime),
    account: Optional[str] = Query(
        None, description="login MT5 de la cuenta (varias cuentas)"),
) -> Dict[str, Any]:
    """Los perfiles definidos y los logins mapeados. Diagnóstico para la UI.

    Con varias cuentas esta respuesta debe incluir los perfiles de TODAS las
    cuentas mapeadas (`PropService.perfiles()` ya lo hace), pero el
    `/state` de esta `?account=` responde solo la del login pedido.
    """
    svc = deps.prop_service(deps.runtime_para_cuenta(rt, account))
    return svc.perfiles()


__all__ = ["router"]
