"""Rutas de auditoría de rendimiento y post-mortem del trading autónomo."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from .. import deps

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/performance")
def get_audit_performance(
    rt: Any = Depends(deps.runtime),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """Analiza las estadísticas históricas de los trades y descartes del bot."""
    store = getattr(rt, "store", None)
    if store is None:
        return {"error": "Store no disponible"}

    conn = store.connect()
    try:
        cur = conn.cursor()

        # 1. Total de setups evaluados
        cur.execute("SELECT COUNT(*) FROM setup_log")
        total_evaluados = cur.fetchone()[0]

        # 2. Total de setups aprobados
        cur.execute("SELECT COUNT(*) FROM setup_log WHERE validated = 1")
        total_aprobados = cur.fetchone()[0]

        # 3. Operaciones ejecutadas registradas en setup_log
        cur.execute("""
            SELECT id, symbol, direction, verdict, score, breakdown_json,
                   entry, sl, target, validated, reject_reasons, trade_result_json,
                   source, created_at
            FROM setup_log
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        filas = cur.fetchall()

        registros = []
        motivos_descarte: Dict[str, int] = {}
        aciertos_por_par: Dict[str, int] = {}

        for f in filas:
            reasons = []
            try:
                reasons = json.loads(f[10]) if f[10] else []
            except Exception:
                pass

            if not f[9]:  # si fue rechazado
                for r in reasons:
                    # Agrupamos por primer segmento de texto
                    key = r.split(":")[0] if ":" in r else r[:35]
                    motivos_descarte[key] = motivos_descarte.get(key, 0) + 1

            registros.append({
                "id": f[0],
                "symbol": f[1],
                "direction": f[2],
                "verdict": f[3],
                "score": f[4],
                "entry": f[6],
                "sl": f[7],
                "target": f[8],
                "validated": bool(f[9]),
                "reasons": reasons,
                "source": f[12],
                "created_at": f[13],
            })

        # 4. Estadísticas de trade marks cerrados (desenlace)
        cur.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN profit < 0 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(profit), 0),
                   COALESCE(AVG(r_multiple), 0)
            FROM trade_marks
        """)
        m_row = cur.fetchone()
        marks_total, wins, losses, total_pnl, avg_r = m_row if m_row else (0, 0, 0, 0.0, 0.0)

        winrate = round((wins / marks_total * 100), 1) if marks_total > 0 else 0.0

        return {
            "summary": {
                "total_setups_evaluated": total_evaluados,
                "total_setups_approved": total_aprobados,
                "closed_trades_total": marks_total,
                "wins": wins,
                "losses": losses,
                "winrate_pct": winrate,
                "total_profit_usd": round(float(total_pnl), 2),
                "avg_r_multiple": round(float(avg_r), 2),
            },
            "top_rejection_reasons": sorted(motivos_descarte.items(), key=lambda x: x[1], reverse=True)[:8],
            "recent_activity": registros[:20],
        }
    finally:
        conn.close()

