"""Rutas para el control del AutoTrader (Bot Autónomo)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import deps

router = APIRouter(prefix="/api/autotrader", tags=["autotrader"])


class AutoTraderToggleBody(BaseModel):
    enabled: bool
    interval_sec: Optional[int] = 30


def _autotrader_svc(rt: Any, account: Optional[str] = None) -> Any:
    # El worker autónomo se asocia a la instancia principal del runtime de la aplicación
    # para que una sola tarea en background opere la sesión activa de MT5
    if not hasattr(rt, "autotrader") or rt.autotrader is None:
        from ..services.autotrader import AutoTraderService
        rt.autotrader = AutoTraderService(
            runtime=rt,
            scan_interval_sec=30,
            account_id=account or getattr(rt, "account_id", None),
        )
    return rt.autotrader


@router.get("/status")
def get_autotrader_status(
    rt: Any = Depends(deps.runtime),
    account: Optional[str] = Query(None, description="Cuenta MT5"),
) -> Dict[str, Any]:
    """Consulta si el bot autónomo está activo y el historial de sus últimas acciones."""
    svc = _autotrader_svc(rt, account)
    return svc.status()


@router.post("/toggle")
async def post_autotrader_toggle(
    body: AutoTraderToggleBody,
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
    account: Optional[str] = Query(None, description="Cuenta MT5"),
) -> Dict[str, Any]:
    """Enciende o apaga el worker autónomo de trading."""
    svc = _autotrader_svc(rt, account)
    if body.enabled:
        if body.interval_sec and body.interval_sec >= 5:
            svc._interval = body.interval_sec
        return await svc.start()
    else:
        return await svc.stop()


@router.post("/cycle")
async def post_autotrader_cycle(
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
    account: Optional[str] = Query(None, description="Cuenta MT5"),
) -> Dict[str, Any]:
    """Ejecuta inmediatamente un ciclo de escaneo, ejecución y gestión sin esperar al timer."""
    svc = _autotrader_svc(rt, account)
    return await svc.run_cycle()

