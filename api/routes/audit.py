"""Rutas de auditoría de rendimiento y post-mortem del trading autónomo."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from .. import deps
from ..runtime import Runtime

log = logging.getLogger("api.routes.audit")
router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/performance")
def get_audit_performance(
    account: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    rt: Runtime = Depends(deps.runtime),
) -> Dict[str, Any]:
    """Analiza las estadísticas históricas de los trades y descartes del bot.
    
    Incluye:
    - Métricas globales de cuenta (winrate, profit USD, avg R, expectancy R, profit factor).
    - Desglose por buckets de score (<60, 60-70, 70-80, 80+).
    - Desglose por símbolo y por motivo de salida (SL, TP, BE, parcial, manual).
    - Descarte de setups de setup_log y actividad reciente.
    """
    ctx_rt = deps.runtime_para_cuenta(rt, account)
    recon = getattr(ctx_rt, "reconciliation", None)
    if recon is not None:
        return recon.get_performance_analysis(limit=limit)

    # Fallback si no está el servicio
    from ..services.reconciliation import ReconciliationService
    recon_fallback = ReconciliationService(market=ctx_rt.market, store=ctx_rt.store, execution=ctx_rt.execution)
    return recon_fallback.get_performance_analysis(limit=limit)


@router.post("/reconcile")
def post_audit_reconcile(
    days: int = Query(30, ge=1, le=365),
    account: Optional[str] = Query(None),
    rt: Runtime = Depends(deps.runtime),
) -> Dict[str, Any]:
    """Fuerza la reconciliación entre los deals cerrados en MT5 y `trade_marks`.
    
    Lee los deals de MT5 (`history_deals_get`), empareja entrada con salida,
    calcula PnL neto, R realizado, clasifica la salida (SL, TP, BE, parcial, manual)
    y vincula con `setup_log` si existe.
    """
    ctx_rt = deps.runtime_para_cuenta(rt, account)
    recon = getattr(ctx_rt, "reconciliation", None)
    if recon is None:
        from ..services.reconciliation import ReconciliationService
        recon = ReconciliationService(market=ctx_rt.market, store=ctx_rt.store, execution=ctx_rt.execution)

    res = recon.reconcile_closed_trades(days=days)
    return res


@router.get("/marks")
def get_trade_marks(
    limit: int = Query(100, ge=1, le=1000),
    account: Optional[str] = Query(None),
    rt: Runtime = Depends(deps.runtime),
) -> List[Dict[str, Any]]:
    """Devuelve las marcas individuales registradas en `trade_marks`."""
    ctx_rt = deps.runtime_para_cuenta(rt, account)
    store = getattr(ctx_rt, "store", None)
    if store is None:
        return []
    try:
        return store.list_trade_marks(limit=limit)
    except Exception as exc:
        log.warning("Error al listar trade_marks: %s", exc)
        return []
