"""Esquema y validación pura para análisis EUR/USD Spot y Tribunal Tripartito.

Sin I/O. Sin red. Sin DB. Solo validación, normalización y defaults deterministas.
"""
from __future__ import annotations

from typing import Any, Dict, List

BIAS = ("alcista", "bajista", "neutral")
SENTIMENT = ("alcista", "bajista", "neutral", "desconocido")
RUN_TIMES = ("07", "11", "14", "18")
VERDICT_STATUS = ("ALTA_CONFLUENCIA", "RIESGO_REDUCIDO", "VETO", "NEUTRAL")
THESIS_DIRECTION = ("BUY", "SELL", "NEUTRAL")
GROUNDING_MODES = (
    "gemini_google_search",
    "rss_plus_gemini",
    "rss_plus_deterministic",
    "deterministic_fallback",
)


class EurUsdSpotAnalysisError(ValueError):
    pass


def _require(d: Dict[str, Any], key: str, ctx: str = "") -> Any:
    if key not in d or d[key] is None:
        raise EurUsdSpotAnalysisError(f"falta campo requerido: {key} {ctx}")
    return d[key]


def _clean_str_list(items: Any, max_items: int = 8) -> List[str]:
    if not isinstance(items, list):
        return []
    out: List[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict) and item.get("text"):
            out.append(str(item["text"]).strip())
        if len(out) >= max_items:
            break
    return out


def validate_tribunal(
    raw: Any,
    default_bias: str = "neutral",
    default_confidence: int = 50,
) -> Dict[str, Any]:
    """Valida y normaliza el bloque del Tribunal (Fiscal vs. Abogado del Diablo -> Juez).

    Mantiene compatibilidad hacia atrás con reportes históricos que no tenían `tribunal`.
    """
    t = raw if isinstance(raw, dict) else {}

    grounding_mode = str(t.get("grounding_mode") or "deterministic_fallback").strip()
    if grounding_mode not in GROUNDING_MODES:
        grounding_mode = "deterministic_fallback"

    search_queries = _clean_str_list(t.get("search_queries"), max_items=10)
    calendar_count = t.get("calendar_events_count")
    rss_count = t.get("rss_headlines_count")
    calendar_events_count = int(calendar_count) if isinstance(calendar_count, (int, float)) and calendar_count >= 0 else 0
    rss_headlines_count = int(rss_count) if isinstance(rss_count, (int, float)) and rss_count >= 0 else 0

    raw_thesis = t.get("thesis") if isinstance(t.get("thesis"), dict) else {}
    default_dir = "BUY" if default_bias == "alcista" else ("SELL" if default_bias == "bajista" else "NEUTRAL")
    direction = str(raw_thesis.get("direction") or default_dir).strip().upper()
    if direction not in THESIS_DIRECTION:
        direction = default_dir

    thesis = {
        "role": str(raw_thesis.get("role") or "Fiscal Técnico (MT5 + SMC)"),
        "direction": direction,
        "headline": str(raw_thesis.get("headline") or f"Postura técnica {default_bias} en EUR/USD").strip(),
        "points": _clean_str_list(raw_thesis.get("points"), max_items=6),
        "smc_summary": str(raw_thesis.get("smc_summary") or "").strip(),
    }

    raw_anti = t.get("antithesis") if isinstance(t.get("antithesis"), dict) else {}
    antithesis = {
        "role": str(raw_anti.get("role") or "Abogado del Diablo (Web + Macro + Riesgo)"),
        "headline": str(raw_anti.get("headline") or "Objeciones macroeconómicas y riesgos de invalidación").strip(),
        "points": _clean_str_list(raw_anti.get("points"), max_items=6),
        "macro_threats": str(raw_anti.get("macro_threats") or "").strip(),
    }

    raw_verdict = t.get("verdict") if isinstance(t.get("verdict"), dict) else {}
    v_status = str(raw_verdict.get("status") or "NEUTRAL").strip().upper()
    if v_status not in VERDICT_STATUS:
        v_status = "NEUTRAL"

    v_bias = str(raw_verdict.get("bias") or default_bias).strip().lower()
    if v_bias not in BIAS:
        v_bias = default_bias if default_bias in BIAS else "neutral"

    raw_conf = raw_verdict.get("confidence")
    if isinstance(raw_conf, (int, float)):
        v_conf = max(0, min(100, int(round(raw_conf))))
    else:
        v_conf = max(0, min(100, int(default_confidence)))

    default_risk_map = {
        "ALTA_CONFLUENCIA": 0.50,
        "RIESGO_REDUCIDO": 0.25,
        "VETO": 0.0,
        "NEUTRAL": 0.25,
    }
    raw_risk = raw_verdict.get("recommended_risk_pct")
    if isinstance(raw_risk, (int, float)) and 0.0 <= float(raw_risk) <= 1.0:
        rec_risk = round(float(raw_risk), 2)
    else:
        rec_risk = default_risk_map[v_status]

    verdict = {
        "role": str(raw_verdict.get("role") or "Juez Cuantitativo"),
        "status": v_status,
        "bias": v_bias,
        "confidence": v_conf,
        "recommended_risk_pct": rec_risk,
        "ruling": str(
            raw_verdict.get("ruling")
            or f"Veredicto {v_status} con sesgo {v_bias} ({v_conf}%)."
        ).strip(),
        "invalidation_condition": str(raw_verdict.get("invalidation_condition") or "").strip(),
    }

    return {
        "grounding_mode": grounding_mode,
        "search_queries": search_queries,
        "calendar_events_count": calendar_events_count,
        "rss_headlines_count": rss_headlines_count,
        "thesis": thesis,
        "antithesis": antithesis,
        "verdict": verdict,
    }


def _normalize_calendar_events(raw_events: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_events, list):
        return []
    out: List[Dict[str, Any]] = []
    for ev in raw_events[:15]:
        if not isinstance(ev, dict):
            continue
        title = str(ev.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "time_utc": str(ev.get("time_utc") or "").strip(),
            "currency": str(ev.get("currency") or "").strip().upper(),
            "impact": str(ev.get("impact") or "Medium").strip(),
            "title": title,
            "forecast": str(ev.get("forecast") or "").strip(),
            "previous": str(ev.get("previous") or "").strip(),
            "minutes_to_event": ev.get("minutes_to_event") if isinstance(ev.get("minutes_to_event"), int) else None,
        })
    return out


def validate_report(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Valida y normaliza el reporte. Lanza EurUsdSpotAnalysisError si inválido."""
    if not isinstance(payload, dict):
        raise EurUsdSpotAnalysisError("payload debe ser dict")

    meta = _require(payload, "meta")
    if not isinstance(meta, dict):
        raise EurUsdSpotAnalysisError("meta debe ser dict")
    _require(meta, "pair")
    _require(meta, "generated_at_utc")
    _require(meta, "run_key")
    rt = meta.get("run_time")
    if rt not in RUN_TIMES:
        raise EurUsdSpotAnalysisError(f"run_time inválido: {rt}")

    bias = _require(payload, "bias")
    if bias not in BIAS:
        raise EurUsdSpotAnalysisError(f"bias inválido: {bias}")

    confidence = _require(payload, "confidence")
    if not isinstance(confidence, int) or isinstance(confidence, bool) or confidence < 0 or confidence > 100:
        raise EurUsdSpotAnalysisError("confidence debe ser int 0-100")

    price = payload.get("price") or {}
    if not isinstance(price, dict):
        raise EurUsdSpotAnalysisError("price debe ser dict")

    fundamental = payload.get("fundamental") or []
    technical = payload.get("technical") or {}
    consensus_vs_dissent = payload.get("consensus_vs_dissent") or {}
    catalysts_today = payload.get("catalysts_today") or []
    risks = payload.get("risks") or []
    sources = payload.get("sources") or []
    calendar_today = _normalize_calendar_events(payload.get("calendar_today"))

    if not isinstance(sources, list) or len(sources) == 0:
        raise EurUsdSpotAnalysisError("se requiere al menos 1 fuente")

    # Normalizar fuentes mínimas
    norm_sources: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    for s in sources:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or s.get("href") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        norm_sources.append({
            "name": str(s.get("name") or s.get("source") or "").strip(),
            "domain": str(s.get("domain") or "").strip(),
            "url": url,
            "title": str(s.get("title") or "").strip(),
            "published_utc": str(s.get("published_utc") or "").strip(),
            "origin": str(s.get("origin") or "web").strip(),
            "sentiment": s.get("sentiment") if s.get("sentiment") in SENTIMENT else "desconocido",
        })
    if not norm_sources:
        raise EurUsdSpotAnalysisError("fuentes sin url válidas")

    tribunal = validate_tribunal(
        payload.get("tribunal"),
        default_bias=bias,
        default_confidence=int(confidence),
    )

    out = {
        "meta": {
            "pair": meta.get("pair") or "EURUSD",
            "generated_at_utc": meta["generated_at_utc"],
            "run_key": meta["run_key"],
            "run_time": rt,
            "timezone": meta.get("timezone") or "UTC",
            "disclaimer": meta.get("disclaimer") or "Esto no es asesoramiento financiero.",
        },
        "price": {
            "current": price.get("current"),
            "bid": price.get("bid"),
            "ask": price.get("ask"),
            "spread_points": price.get("spread_points"),
            "change24h": price.get("change24h"),
            "high24h": price.get("high24h"),
            "low24h": price.get("low24h"),
            "source": price.get("source") or "",
        },
        "bias": bias,
        "confidence": int(confidence),
        "tribunal": tribunal,
        "calendar_today": calendar_today,
        "fundamental": fundamental if isinstance(fundamental, list) else [],
        "technical": technical if isinstance(technical, dict) else {},
        "consensus_vs_dissent": consensus_vs_dissent if isinstance(consensus_vs_dissent, dict) else {},
        "catalysts_today": catalysts_today if isinstance(catalysts_today, list) else [],
        "risks": risks if isinstance(risks, list) else [],
        "sources": norm_sources,
        "raw_summary_md": payload.get("raw_summary_md") or payload.get("md_report") or "",
    }
    return out


def count_consensus(sources: List[Dict[str, Any]]) -> Dict[str, int]:
    bull = bear = neu = 0
    for s in sources:
        st = s.get("sentiment") or "desconocido"
        if st == "alcista":
            bull += 1
        elif st == "bajista":
            bear += 1
        elif st == "neutral":
            neu += 1
    return {"bull": bull, "bear": bear, "neutral": neu}
