"""Rutas para análisis EUR/USD spot y Tribunal Tripartito."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from api.services.eurusd_spot_analysis_service import EurUsdSpotAnalysisService
from core.bank_consensus import load_bank_analyses_data
from core.daily_guide import compute_daily_guide
from core.eurusd_spot_analysis_schema import count_consensus

router = APIRouter(prefix="/api/analysis/eurusd-spot", tags=["analysis-eurusd-spot"])
svc = EurUsdSpotAnalysisService()


def _ensure_daily_guide(payload: Dict[str, Any]) -> None:
    if not payload.get("daily_guide"):
        p_val = (payload.get("price") or {}).get("current") or 1.1238
        tech_data = payload.get("technical") or {}
        smc_data = tech_data.get("smc_summary") or {}
        pdh_val = smc_data.get("pdh")
        pdl_val = smc_data.get("pdl")
        payload["daily_guide"] = compute_daily_guide(
            current_price=p_val,
            pdh=pdh_val,
            pdl=pdl_val,
            calendar_events=payload.get("calendar_today"),
        )


@router.get("/daily-guide")
def daily_guide_route() -> Dict[str, Any]:
    """Retorna la Guía Operativa Diaria en vivo con niveles de PDH/PDL y 50% Equilibrium."""
    row = svc.get_latest()
    if row:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
            _ensure_daily_guide(payload)
            return payload.get("daily_guide") or {}
        except Exception:
            pass
    return compute_daily_guide(1.1238, 1.1275, 1.1205)


@router.get("/bank-consensus")
def bank_consensus() -> Dict[str, Any]:
    """Retorna el meta-análisis y consenso estructurado de los top 10 bancos y analistas."""
    return load_bank_analyses_data()


@router.get("/latest")
def latest() -> Dict[str, Any]:
    row = svc.get_latest()
    if not row:
        raise HTTPException(status_code=404, detail="sin análisis aún")
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except Exception:
        payload = {}
    if not payload.get("bank_consensus"):
        payload["bank_consensus"] = load_bank_analyses_data()
    _ensure_daily_guide(payload)
    payload["_meta_db"] = {
        "id": row.get("id"),
        "run_key": row.get("run_key"),
        "run_time": row.get("run_time"),
        "generated_at_utc": row.get("generated_at_utc"),
        "consensus_bull": row.get("consensus_bull"),
        "consensus_bear": row.get("consensus_bear"),
        "consensus_neutral": row.get("consensus_neutral"),
        "sources_count": row.get("sources_count"),
    }
    return payload


@router.get("/by-run")
def by_run(run_key: str = Query(...)) -> Dict[str, Any]:
    row = svc.get_by_run_key(run_key)
    if not row:
        raise HTTPException(status_code=404, detail="no encontrado")
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except Exception:
        payload = {}
    if not payload.get("bank_consensus"):
        payload["bank_consensus"] = load_bank_analyses_data()
    _ensure_daily_guide(payload)
    payload["_meta_db"] = {
        "id": row.get("id"),
        "run_key": row.get("run_key"),
        "run_time": row.get("run_time"),
        "generated_at_utc": row.get("generated_at_utc"),
        "consensus_bull": row.get("consensus_bull"),
        "consensus_bear": row.get("consensus_bear"),
        "consensus_neutral": row.get("consensus_neutral"),
        "sources_count": row.get("sources_count"),
    }
    return payload


@router.get("/history")
def history(limit: int = Query(50, ge=1, le=200), run_time: Optional[str] = Query(None)) -> Dict[str, Any]:
    rows = svc.get_history(limit=limit, run_time=run_time)
    items = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except Exception:
            payload = {}
        trib = payload.get("tribunal") if isinstance(payload, dict) else {}
        trib = trib if isinstance(trib, dict) else {}
        verdict = trib.get("verdict") if isinstance(trib.get("verdict"), dict) else {}
        items.append({
            "run_key": row.get("run_key"),
            "run_time": row.get("run_time"),
            "generated_at_utc": row.get("generated_at_utc"),
            "bias": row.get("bias"),
            "confidence": row.get("confidence"),
            "price_current": row.get("price_current"),
            "sources_count": row.get("sources_count"),
            "consensus_bull": row.get("consensus_bull"),
            "consensus_bear": row.get("consensus_bear"),
            "consensus_neutral": row.get("consensus_neutral"),
            "verdict_status": verdict.get("status") or "NEUTRAL",
            "recommended_risk_pct": verdict.get("recommended_risk_pct"),
            "grounding_mode": trib.get("grounding_mode") or "deterministic_fallback",
            "md_report": row.get("md_report"),
            "summary": payload.get("meta"),
        })
    return {"items": items, "count": len(items)}


@router.get("/runs/today")
def runs_today() -> Dict[str, Any]:
    rows = svc.get_today()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.post("/run")
def run_manual(
    request: Request,
    force: bool = Query(True),
) -> Dict[str, Any]:
    rt = getattr(request.app.state, "runtime", None) if hasattr(request, "app") else None
    market_svc = getattr(rt, "market", None) if rt else None
    try:
        report = svc.generate_analysis(market_service=market_svc, force=force)
        meta = report.get("meta") or {}
        cons = count_consensus(report.get("sources") or [])
        report["_meta_db"] = {
            "run_key": meta.get("run_key"),
            "run_time": meta.get("run_time"),
            "generated_at_utc": meta.get("generated_at_utc"),
            "consensus_bull": cons["bull"],
            "consensus_bear": cons["bear"],
            "consensus_neutral": cons["neutral"],
            "sources_count": len(report.get("sources") or []),
        }
        return report
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"error al generar análisis: {exc}")
