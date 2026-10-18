""" Inteligencia web en vivo y Tribunal Grounded (Gemini + Google Search + RSS + Calendario) para EUR/USD.

Capa de I/O (`api/services/`):
  1. Recolecta titulares institucionales reales por RSS (BCE, Fed, ForexLive, FXStreet) en paralelo.
  2. Recolecta el calendario macroeconómico de hoy para EUR y USD (eventos Medium/High impact).
  3. Ejecuta el debate del Tribunal Tripartito (Fiscal vs. Abogado del Diablo -> Juez) en Gemini
     con la herramienta nativa `google_search` habilitada, extrayendo citas reales (`url_citation` / `groundingChunks`).
  4. Nunca bloquea ni rompe el sistema: degrada con elegancia si un feed falla o no hay red.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests

TRUSTED_DOMAINS: Tuple[str, ...] = (
    "ecb.europa.eu",
    "federalreserve.gov",
    "bls.gov",
    "bea.gov",
    "europa.eu",
    "imf.org",
    "bis.org",
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "cnbc.com",
    "fxstreet.com",
    "forexlive.com",
    "investinglive.com",
    "dailyfx.com",
    "investing.com",
    "tradingeconomics.com",
    "marketwatch.com",
    "forexfactory.com",
    "faireconomy.media",
)

RSS_FEEDS: Tuple[Dict[str, str], ...] = (
    {
        "name": "Banco Central Europeo (BCE)",
        "domain": "ecb.europa.eu",
        "url": "https://www.ecb.europa.eu/rss/press.html",
        "kind": "central_bank",
    },
    {
        "name": "Federal Reserve (FOMC)",
        "domain": "federalreserve.gov",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "kind": "central_bank",
    },
    {
        "name": "FXStreet Forex News",
        "domain": "fxstreet.com",
        "url": "https://xml.fxstreet.com/news/forex-news/index.xml",
        "kind": "fx_wire",
    },
    {
        "name": "ForexLive / InvestingLive",
        "domain": "forexlive.com",
        "url": "https://www.forexlive.com/feed/news",
        "kind": "fx_wire",
    },
)

CALENDAR_JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

FX_RELEVANT_KEYWORDS: Tuple[str, ...] = (
    "eur/usd",
    "eurusd",
    "euro",
    "ecb",
    "lagarde",
    "bund",
    "eurozone",
    "german",
    "fed",
    "fomc",
    "powell",
    "dollar",
    "usd",
    "dxy",
    "treasury",
    "yield",
    "cpi",
    "inflation",
    "pce",
    "nfp",
    "payroll",
    "pmi",
    "gdp",
    "rate",
    "tariff",
)

_HTTP_HEADERS = {
    "User-Agent": "FTMOCopilotDashboard/1.0 (+Institutional-FX-Research)",
    "Accept": "application/rss+xml, application/xml, text/xml, application/json, */*",
}


def extract_domain(url: str, fallback_title: str = "") -> str:
    """Extrae el dominio limpio de una URL (o del título si es un redirect de Google Grounding)."""
    try:
        host = (urlparse(url).netloc or "").lower().strip()
        if host.startswith("www."):
            host = host[4:]
        if "vertexaisearch.cloud.google.com" in host or "google.com" in host:
            cand = fallback_title.strip().lower()
            if "." in cand and " " not in cand:
                return cand[4:] if cand.startswith("www.") else cand
        return host
    except Exception:
        return ""


def is_trusted_domain(domain: str) -> bool:
    d = (domain or "").lower().strip()
    if not d:
        return False
    return any(d == td or d.endswith("." + td) for td in TRUSTED_DOMAINS)


def infer_headline_sentiment(title: str) -> str:
    """Heurística ligera y determinista de sentimiento EUR/USD para titulares RSS."""
    t = (title or "").lower()
    bull_eur = (
        "eur/usd rises",
        "eur/usd climbs",
        "eur/usd gains",
        "eur/usd rebounds",
        "eur/usd surges",
        "euro gains",
        "euro rises",
        "euro strengthens",
        "dollar weakens",
        "dollar drops",
        "dollar slides",
        "soften usd",
        "dovish fed",
        "hawkish ecb",
        "ecb rate hike",
        "fed rate cut",
    )
    bear_eur = (
        "eur/usd falls",
        "eur/usd drops",
        "eur/usd slides",
        "eur/usd sinks",
        "eur/usd retreats",
        "eur/usd under pressure",
        "euro falls",
        "euro weakens",
        "euro drops",
        "dollar strengthens",
        "dollar rises",
        "dollar surges",
        "stronger dollar",
        "hawkish fed",
        "dovish ecb",
        "ecb rate cut",
    )
    b_pos = sum(1 for k in bull_eur if k in t)
    b_neg = sum(1 for k in bear_eur if k in t)
    if b_pos > b_neg:
        return "alcista"
    if b_neg > b_pos:
        return "bajista"
    return "neutral"


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    clean = re.sub(r"<[^>]+>", " ", raw)
    clean = html.unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def _parse_rss_date(raw_date: str) -> Optional[datetime]:
    val = (raw_date or "").strip()
    if not val:
        return None
    try:
        dt = parsedate_to_datetime(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        iso = val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_rss_xml(
    xml_text: str,
    feed_meta: Dict[str, str],
    now_utc: Optional[datetime] = None,
    max_items: int = 5,
) -> List[Dict[str, Any]]:
    """Parsea XML RSS 2.0 / RDF / Atom y filtra por relevancia EUR/USD y frescura."""
    if not xml_text or not xml_text.strip():
        return []
    now = now_utc or datetime.now(timezone.utc)
    max_age_hours = 120 if feed_meta.get("kind") == "central_bank" else 48
    cutoff = now - timedelta(hours=max_age_hours)

    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return []

    results: List[Dict[str, Any]] = []
    # Soporta <item> (RSS 2.0 / RDF) y <entry> (Atom) con o sin namespace
    nodes = [el for el in root.iter() if el.tag.endswith("item") or el.tag.endswith("entry")]

    for node in nodes:
        title = ""
        link = ""
        pub_raw = ""
        desc = ""
        for child in node:
            tag = child.tag.split("}")[-1].lower()
            if tag == "title" and not title:
                title = _strip_html(child.text or "")
            elif tag == "link" and not link:
                href = child.attrib.get("href")
                link = (href or child.text or "").strip()
            elif tag in ("pubdate", "published", "updated", "date") and not pub_raw:
                pub_raw = (child.text or "").strip()
            elif tag in ("description", "summary") and not desc:
                desc = _strip_html(child.text or "")[:240]

        if not title or not link or not link.startswith("http"):
            continue

        pub_dt = _parse_rss_date(pub_raw)
        if pub_dt is not None and pub_dt < cutoff:
            continue

        combined_lower = f"{title} {desc}".lower()
        if feed_meta.get("kind") != "central_bank":
            if not any(kw in combined_lower for kw in FX_RELEVANT_KEYWORDS):
                continue

        dom = extract_domain(link) or feed_meta.get("domain", "")
        results.append({
            "name": feed_meta.get("name", dom),
            "domain": dom,
            "url": link,
            "title": title,
            "summary": desc,
            "published_utc": pub_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if pub_dt else "",
            "origin": "rss",
            "sentiment": infer_headline_sentiment(title),
        })
        if len(results) >= max_items:
            break

    return results


def parse_calendar_json(
    items: Any,
    now_utc: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Filtra eventos macroeconómicos EUR y USD de impacto High/Medium en ventana [-6h, +24h]."""
    if not isinstance(items, list):
        return []
    now = now_utc or datetime.now(timezone.utc)
    start_win = now - timedelta(hours=6)
    end_win = now + timedelta(hours=24)

    events: List[Dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        country = str(raw.get("country") or "").strip().upper()
        if country not in ("USD", "EUR"):
            continue
        impact = str(raw.get("impact") or "").strip()
        if impact not in ("High", "Medium"):
            continue
        title = str(raw.get("title") or "").strip()
        date_str = str(raw.get("date") or "").strip()
        if not title or not date_str:
            continue
        ev_dt = _parse_rss_date(date_str)
        if ev_dt is None or ev_dt < start_win or ev_dt > end_win:
            continue
        mins = int(round((ev_dt - now).total_seconds() / 60.0))
        events.append({
            "time_utc": ev_dt.strftime("%H:%M UTC"),
            "timestamp_utc": ev_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "currency": country,
            "impact": impact,
            "title": title,
            "forecast": str(raw.get("forecast") or "").strip(),
            "previous": str(raw.get("previous") or "").strip(),
            "minutes_to_event": mins,
        })

    events.sort(key=lambda x: x.get("timestamp_utc", ""))
    return events[:12]


def collect_live_web_context(
    now_utc: Optional[datetime] = None,
    timeout_s: float = 4.0,
    http_get: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Recolecta en paralelo RSS institucionales y calendario económico EUR/USD."""
    now = now_utc or datetime.now(timezone.utc)
    getter = http_get or requests.get

    rss_items: List[Dict[str, Any]] = []
    calendar_events: List[Dict[str, Any]] = []
    errors: List[str] = []

    def _fetch_rss(feed: Dict[str, str]) -> List[Dict[str, Any]]:
        resp = getter(feed["url"], headers=_HTTP_HEADERS, timeout=timeout_s)
        if resp.status_code != 200:
            return []
        return parse_rss_xml(resp.text, feed, now_utc=now, max_items=4)

    def _fetch_cal() -> List[Dict[str, Any]]:
        resp = getter(CALENDAR_JSON_URL, headers=_HTTP_HEADERS, timeout=timeout_s)
        if resp.status_code != 200:
            return []
        return parse_calendar_json(resp.json(), now_utc=now)

    with ThreadPoolExecutor(max_workers=5) as pool:
        future_map: Dict[Any, str] = {pool.submit(_fetch_rss, f): f["name"] for f in RSS_FEEDS}
        future_map[pool.submit(_fetch_cal)] = "Calendar"

        for fut in as_completed(future_map):
            label = future_map[fut]
            try:
                res = fut.result()
                if label == "Calendar":
                    calendar_events = res
                else:
                    rss_items.extend(res)
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}")

    # Ordenar RSS poniendo primero titulares más recientes
    rss_items.sort(key=lambda x: x.get("published_utc") or "", reverse=True)
    return {
        "rss_items": rss_items[:12],
        "calendar_events": calendar_events,
        "errors": errors,
    }


def extract_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    """Extrae de forma resiliente el primer objeto JSON válido de la respuesta del modelo."""
    if not raw_text:
        return None
    txt = raw_text.strip()
    if txt.startswith("```"):
        parts = txt.split("```")
        for p in parts[1:]:
            candidate = p.strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                txt = candidate
                break

    try:
        parsed = json.loads(txt)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    start = txt.find("{")
    end = txt.rfind("}")
    if start >= 0 and end > start:
        snippet = txt[start : end + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
    return None


def _build_tribunal_prompt(
    now_iso: str,
    price_info: Dict[str, Any],
    m15_inds: Dict[str, Any],
    h1_inds: Dict[str, Any],
    levels: Dict[str, Any],
    smc_summary: Dict[str, Any],
    rss_items: List[Dict[str, Any]],
    calendar_events: List[Dict[str, Any]],
) -> str:
    rss_lines = [
        f"- [{it.get('domain')}] {it.get('title')} ({it.get('published_utc') or 'reciente'}) -> {it.get('url')}"
        for it in rss_items[:10]
    ] or ["- Sin titulares RSS previos; usa obligatoriamente Google Search para buscar noticias de hoy sobre EUR/USD, BCE, Fed y DXY."]

    cal_lines = [
        f"- {ev.get('time_utc')} | {ev.get('currency')} | Impacto {ev.get('impact')} | {ev.get('title')} "
        f"(Prev: {ev.get('previous') or '-'}, Est: {ev.get('forecast') or '-'}, en {ev.get('minutes_to_event')} min)"
        for ev in calendar_events[:10]
    ] or ["- Sin eventos High/Medium filtrados en feed local; verifica con Google Search el calendario económico EUR y USD de hoy."]

    return (
        f"Fecha/Hora UTC actual: {now_iso}\n"
        f"Actúa como el **TRIBUNAL TRIPARTITO INSTITUCIONAL EUR/USD** para una cuenta de prop firm FTMO ($25k).\n"
        f"Usa la herramienta Google Search para verificar las noticias macroeconómicas MÁS RECIENTES de HOY sobre EUR/USD, "
        f"DXY, rendimientos US10Y vs Bund alemán 10Y, Reserva Federal (Fed) y Banco Central Europeo (BCE).\n\n"
        f"=== EVIDENCIA 1: DATOS VIVOS DE METATRADER 5 Y SMC ===\n"
        f"- Precio actual EUR/USD: {price_info.get('current')} (Bid: {price_info.get('bid')}, Ask: {price_info.get('ask')}, "
        f"Spread: {price_info.get('spread_points')} pts, Fuente: {price_info.get('source')})\n"
        f"- Rango 24h: Cambio {price_info.get('change24h')}%, Máx 24h: {price_info.get('high24h')}, Mín 24h: {price_info.get('low24h')}\n"
        f"- Indicadores M15: SMA20={m15_inds.get('sma20')}, SMA50={m15_inds.get('sma50')}, SMA100={m15_inds.get('sma100')}, "
        f"SMA200={m15_inds.get('sma200')}, RSI14={m15_inds.get('rsi14')}, MACD={json.dumps(m15_inds.get('macd'))}\n"
        f"- Indicadores H1: SMA50={h1_inds.get('sma50')}, SMA100={h1_inds.get('sma100')}, RSI14={h1_inds.get('rsi14')}\n"
        f"- Microestructura SMC y PDH/PDL: {json.dumps(smc_summary, ensure_ascii=False)}\n"
        f"- Soportes técnicos: {levels.get('supports')} | Resistencias técnicas: {levels.get('resistances')}\n\n"
        f"=== EVIDENCIA 2: TITULARES RSS INSTITUCIONALES RECIENTES ===\n"
        + "\n".join(rss_lines)
        + "\n\n=== EVIDENCIA 3: CALENDARIO MACROECONÓMICO HOY (EUR / USD) ===\n"
        + "\n".join(cal_lines)
        + "\n\n=== INSTRUCCIONES DEL DEBATE ADVERSARIAL ===\n"
        "1. **Fiscal Técnico (thesis)**: Construye el mejor caso direccional posible basado estrictamente en los precios de MT5, "
        "zonas SMC (FVG, Order Blocks, Sweeps), alineación M15/H1 y niveles PDH/PDL. Cita precios exactos.\n"
        "2. **Abogado del Diablo (antithesis)**: Ataca sin piedad la tesis del Fiscal usando noticias reales encontradas hoy en la web, "
        "diferencial de tasas Fed/BCE, eventos del calendario económico y trampas técnicas (divergencia M15 vs H1, RSI extremo, bloques opuestos).\n"
        "3. **Juez Cuantitativo (verdict)**: Contrapone ambos argumentos y dicta sentencia imparcial:\n"
        "   - `status`: \"ALTA_CONFLUENCIA\" (riesgo 0.50%), \"RIESGO_REDUCIDO\" (riesgo 0.25%), \"VETO\" (riesgo 0.0% por noticia explosiva inminente o contradicción grave), o \"NEUTRAL\".\n"
        "   - `bias`: \"alcista\", \"bajista\" o \"neutral\".\n"
        "   - `confidence`: entero 0-100.\n"
        "   - `recommended_risk_pct`: 0.5, 0.25 o 0.0.\n"
        "   - `ruling`: dictamen ejecutivo claro en español explicando quién ganó el debate y por qué.\n"
        "   - `invalidation_condition`: nivel de precio o evento exacto que invalida el escenario.\n\n"
        "Responde ÚNICAMENTE con un objeto JSON válido (sin texto adicional fuera del JSON) con esta estructura exacta:\n"
        "{\n"
        '  "bias": "alcista|bajista|neutral",\n'
        '  "confidence": 68,\n'
        '  "thesis": {\n'
        '    "direction": "BUY|SELL|NEUTRAL",\n'
        '    "headline": "Titular conciso de la tesis técnica",\n'
        '    "points": ["Punto 1 con niveles MT5/SMC", "Punto 2", "Punto 3"]\n'
        "  },\n"
        '  "antithesis": {\n'
        '    "headline": "Titular de las objeciones web/macro y técnicas",\n'
        '    "points": ["Objeción 1 con dato macro/web", "Objeción 2", "Objeción 3"],\n'
        '    "macro_threats": "Resumen corto de amenazas macro/calendario hoy"\n'
        "  },\n"
        '  "verdict": {\n'
        '    "status": "ALTA_CONFLUENCIA|RIESGO_REDUCIDO|VETO|NEUTRAL",\n'
        '    "bias": "alcista|bajista|neutral",\n'
        '    "confidence": 68,\n'
        '    "recommended_risk_pct": 0.25,\n'
        '    "ruling": "Dictamen final del Juez contraponiendo Tesis vs Antítesis",\n'
        '    "invalidation_condition": "Nivel exacto de invalidación en EUR/USD"\n'
        "  },\n"
        '  "fundamental": ["Factor macro 1 verificado hoy", "Factor macro 2", "Factor macro 3", "Factor macro 4"],\n'
        '  "catalysts_today": ["Catalizador 1 de hoy con hora/contexto", "Catalizador 2", "Catalizador 3"],\n'
        '  "risks": ["Riesgo operativo 1", "Riesgo 2", "Riesgo 3"],\n'
        '  "consensus_vs_dissent": "Síntesis del consenso frente al escenario alternativo",\n'
        '  "sources_evaluated": [\n'
        '    {"name": "Nombre medio", "url": "https://...", "title": "Titular real verificado", "sentiment": "alcista|bajista|neutral"}\n'
        "  ],\n"
        '  "raw_summary_md": "# Tribunal EUR/USD Spot\\n\\nResumen ejecutivo en Markdown con secciones Tesis, Antítesis, Veredicto y Niveles."\n'
        "}"
    )


def _call_interactions_api(
    api_key: str,
    model: str,
    prompt: str,
    timeout_s: float,
    http_post: Callable[..., Any],
    use_search_tool: bool = True,
) -> Optional[Dict[str, Any]]:
    """Llama a Gemini Interactions API (`v1beta/interactions`), con o sin `google_search`."""
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "Api-Revision": "2026-05-20",
    }
    body: Dict[str, Any] = {
        "model": model,
        "input": prompt,
    }
    if use_search_tool:
        body["tools"] = [{"type": "google_search"}]
    resp = http_post(url, headers=headers, json=body, timeout=timeout_s)
    if resp.status_code != 200:
        return None
    data = resp.json()
    text_parts: List[str] = []
    citations: List[Dict[str, str]] = []
    queries: List[str] = []

    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        text_parts.append(data["output_text"])

    for step in data.get("steps") or []:
        if not isinstance(step, dict):
            continue
        stype = step.get("type")
        if stype == "google_search_call":
            q_list = (step.get("arguments") or {}).get("queries") or []
            for q in q_list:
                if isinstance(q, str) and q.strip():
                    queries.append(q.strip())
        elif stype == "model_output":
            for block in step.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if not text_parts and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                for ann in block.get("annotations") or []:
                    if isinstance(ann, dict) and ann.get("url"):
                        u = str(ann["url"]).strip()
                        t = str(ann.get("title") or "").strip()
                        dom = extract_domain(u, t)
                        citations.append({
                            "name": t or dom or "Google Search Grounding",
                            "domain": dom,
                            "url": u,
                            "title": t or u,
                            "origin": "google_search",
                        })

    if not text_parts:
        for out_item in data.get("outputs") or []:
            if isinstance(out_item, dict) and isinstance(out_item.get("text"), str):
                text_parts.append(out_item["text"])

    full_text = "\n".join(text_parts).strip()
    if not full_text:
        return None
    return {
        "text": full_text,
        "citations": citations,
        "queries": queries,
        "used_search_tool": use_search_tool,
    }


def _call_generate_content_api(
    api_key: str,
    model: str,
    prompt: str,
    timeout_s: float,
    http_post: Callable[..., Any],
    use_search_tool: bool = True,
) -> Optional[Dict[str, Any]]:
    """Respaldo por `models/{model}:generateContent` con o sin `google_search`."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    body: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    if use_search_tool:
        body["tools"] = [{"google_search": {}}]
    resp = http_post(url, headers=headers, json=body, timeout=timeout_s)
    if resp.status_code != 200:
        return None
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return None

    cand0 = candidates[0] if isinstance(candidates[0], dict) else {}
    parts = ((cand0.get("content") or {}).get("parts")) or []
    text_parts = [str(p.get("text")) for p in parts if isinstance(p, dict) and p.get("text")]
    full_text = "\n".join(text_parts).strip()
    if not full_text:
        return None

    gm = cand0.get("groundingMetadata") or {}
    queries = [str(q).strip() for q in (gm.get("webSearchQueries") or []) if str(q).strip()]
    citations: List[Dict[str, str]] = []
    for chunk in gm.get("groundingChunks") or []:
        web = (chunk or {}).get("web") if isinstance(chunk, dict) else None
        if isinstance(web, dict) and web.get("uri"):
            u = str(web["uri"]).strip()
            t = str(web.get("title") or "").strip()
            dom = extract_domain(u, t)
            citations.append({
                "name": dom or t or "Google Search Grounding",
                "domain": dom,
                "url": u,
                "title": t or dom or u,
                "origin": "google_search",
            })

    return {
        "text": full_text,
        "citations": citations,
        "queries": queries,
        "used_search_tool": use_search_tool,
    }


def resolve_gemini_models() -> List[str]:
    """Ordena los modelos Gemini actuales limpiando prefijos `gemini/` y descartando modelos 2.x retirados."""
    configured = (os.getenv("GEMINI_MODEL") or os.getenv("LITELLM_MODEL") or "").strip()
    if configured.startswith("gemini/"):
        configured = configured[len("gemini/") :]
    # gemini-2.5-* devuelve 404 para cuentas actuales; redirigir limpiamente a la serie 3.x vigente
    if configured.startswith(("gemini-1.", "gemini-2.")):
        configured = "gemini-3.8-flash"
    candidates: List[str] = []
    for m in (configured, "gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
        if m and m not in candidates:
            candidates.append(m)
    return candidates


def run_gemini_grounded_tribunal(
    api_key: str,
    now_iso: str,
    price_info: Dict[str, Any],
    m15_inds: Dict[str, Any],
    h1_inds: Dict[str, Any],
    levels: Dict[str, Any],
    smc_summary: Dict[str, Any],
    rss_items: List[Dict[str, Any]],
    calendar_events: List[Dict[str, Any]],
    timeout_s: float = 18.0,
    http_post: Optional[Callable[..., Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Ejecuta el Tribunal Tripartito en Gemini:
    1º Intenta con `google_search` activo (grounding web completo).
    2º Si el plan gratuito devuelve 429 en `google_search` o 503 en un modelo, cae automáticamente
       a deliberación IA sobre los feeds RSS + Calendario en vivo ya recolectados (`rss_plus_gemini`).
    """
    if not api_key:
        return None
    poster = http_post or requests.post
    prompt = _build_tribunal_prompt(
        now_iso=now_iso,
        price_info=price_info,
        m15_inds=m15_inds,
        h1_inds=h1_inds,
        levels=levels,
        smc_summary=smc_summary,
        rss_items=rss_items,
        calendar_events=calendar_events,
    )

    models = resolve_gemini_models()
    raw_result: Optional[Dict[str, Any]] = None
    used_model = ""

    # Pasada 1: Con herramienta nativa Google Search Grounding
    # Pasada 2: Sin herramienta Google Search (usa los RSS + Calendario reales ya recolectados; funciona en Free Tier)
    for use_search in (True, False):
        for model in models:
            try:
                raw_result = _call_interactions_api(
                    api_key, model, prompt, timeout_s, poster, use_search_tool=use_search
                )
                if raw_result is None and not use_search:
                    raw_result = _call_generate_content_api(
                        api_key, model, prompt, timeout_s, poster, use_search_tool=False
                    )
                if raw_result is not None:
                    parsed_try = extract_json_object(raw_result["text"])
                    if parsed_try:
                        used_model = model
                        return {
                            "model": used_model,
                            "parsed": parsed_try,
                            "citations": raw_result.get("citations") or [],
                            "queries": raw_result.get("queries") or [],
                            "used_search_tool": use_search,
                        }
            except Exception:
                continue

    return None


def merge_grounded_sources(
    rss_items: List[Dict[str, Any]],
    gemini_citations: List[Dict[str, Any]],
    llm_sources: List[Dict[str, Any]],
    default_bias: str = "neutral",
) -> List[Dict[str, Any]]:
    """Combina y deduplica fuentes reales verificadas (Google Search Grounding + RSS institucional).

    Solo recurre a portales institucionales base si no hubo ni RSS ni citas web (modo 100% offline).
    """
    merged: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()

    # Mapa rápido de sentimiento sugerido por el LLM por URL o dominio
    llm_sent_by_url: Dict[str, str] = {}
    llm_title_by_url: Dict[str, str] = {}
    for s in llm_sources or []:
        if not isinstance(s, dict):
            continue
        u = str(s.get("url") or "").strip()
        st = str(s.get("sentiment") or "").strip().lower()
        if u and st in ("alcista", "bajista", "neutral"):
            llm_sent_by_url[u] = st
        if u and s.get("title"):
            llm_title_by_url[u] = str(s["title"]).strip()

    # 1. Titulares RSS institucionales verificados directamente (tienen URL limpia, fecha y titular exacto)
    for item in rss_items:
        u = str(item.get("url") or "").strip()
        if not u or u in seen_urls:
            continue
        seen_urls.add(u)
        st = llm_sent_by_url.get(u) or item.get("sentiment") or "neutral"
        merged.append({
            "name": item.get("name") or item.get("domain") or "RSS Institucional",
            "domain": item.get("domain") or extract_domain(u),
            "url": u,
            "title": item.get("title") or u,
            "published_utc": item.get("published_utc") or "",
            "origin": "rss",
            "sentiment": st,
        })

    # 2. Citas reales devueltas por Google Search Grounding Metadata
    for cit in gemini_citations:
        u = str(cit.get("url") or "").strip()
        if not u or u in seen_urls:
            continue
        dom = cit.get("domain") or extract_domain(u, str(cit.get("title") or ""))
        seen_urls.add(u)
        title = llm_title_by_url.get(u) or str(cit.get("title") or dom or "Fuente Web Grounded").strip()
        st = llm_sent_by_url.get(u) or infer_headline_sentiment(title)
        merged.append({
            "name": f"{dom} (Google Search)" if dom else "Google Search Grounding",
            "domain": dom,
            "url": u,
            "title": title,
            "published_utc": "",
            "origin": "google_search",
            "sentiment": st,
        })

    # 3. Fuentes adicionales citadas por el LLM SOLO si pertenecen a dominios institucionales de confianza
    for s in llm_sources or []:
        if not isinstance(s, dict):
            continue
        u = str(s.get("url") or "").strip()
        if not u or not u.startswith("https://") or u in seen_urls:
            continue
        dom = extract_domain(u)
        if not is_trusted_domain(dom):
            continue
        seen_urls.add(u)
        title = str(s.get("title") or dom).strip()
        st = str(s.get("sentiment") or "").strip().lower()
        if st not in ("alcista", "bajista", "neutral"):
            st = infer_headline_sentiment(title)
        merged.append({
            "name": str(s.get("name") or dom).strip(),
            "domain": dom,
            "url": u,
            "title": title,
            "published_utc": "",
            "origin": "gemini_cited",
            "sentiment": st,
        })

    # Ordenar priorizando dominios de nuestra whitelist institucional
    merged.sort(key=lambda s: (0 if is_trusted_domain(s.get("domain", "")) else 1))

    if merged:
        return merged[:12]

    # 4. Fallback exclusivamente offline (sin red ni RSS ni Gemini)
    return [
        {
            "name": "Banco Central Europeo (Portal Oficial - Modo Offline)",
            "domain": "ecb.europa.eu",
            "url": "https://www.ecb.europa.eu/press/pr/date/html/index.en.html",
            "title": "Referencia institucional BCE (sin conexión a feeds en vivo en esta ejecución)",
            "published_utc": "",
            "origin": "offline_fallback",
            "sentiment": "neutral",
        },
        {
            "name": "Federal Reserve FOMC (Portal Oficial - Modo Offline)",
            "domain": "federalreserve.gov",
            "url": "https://www.federalreserve.gov/newsevents/pressreleases.htm",
            "title": "Referencia institucional Reserva Federal (sin conexión a feeds en vivo en esta ejecución)",
            "published_utc": "",
            "origin": "offline_fallback",
            "sentiment": default_bias if default_bias in ("alcista", "bajista") else "neutral",
        },
    ]
