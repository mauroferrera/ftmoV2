"""Rutas para análisis EUR/USD spot y Tribunal Tripartito."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from api.services.eurusd_spot_analysis_service import EurUsdSpotAnalysisService
from core.eurusd_spot_analysis_schema import count_consensus

router = APIRouter(prefix="/api/analysis/eurusd-spot", tags=["analysis-eurusd-spot"])
svc = EurUsdSpotAnalysisService()


@router.get("/latest")
def latest() -> Dict[str, Any]:
    row = svc.get_latest()
    if not row:
        raise HTTPException(status_code=404, detail="sin análisis aún")
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except Exception:
        payload = {}
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
