"""Servicio para análisis EUR/USD Spot y Tribunal Tripartito (MT5/SMC + Web Grounding + IA).

Orquesta:
  1. Extracción viva desde MetaTrader 5 (tick Bid/Ask, spread, M15 + H1, PDH/PDL y zonas SMC).
  2. Recolección web en tiempo real (feeds RSS institucionales BCE/Fed/FXStreet/ForexLive + Calendario Macro).
  3. Debate adversarial en Gemini con Google Search Grounding (Fiscal vs. Abogado del Diablo -> Juez),
     con respaldo determinista experto cuando no hay red o API key.
  4. Validación pura (`core.eurusd_spot_analysis_schema`) y persistencia en SQLite (`trading.db`).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from typing import Any, Callable, Dict, List, Optional

from api.services.eurusd_web_intel import (
    collect_live_web_context,
    merge_grounded_sources,
    run_gemini_grounded_tribunal,
)
from core import clock, paths
from core.eurusd_spot_analysis_schema import count_consensus, validate_report
from core.eurusd_spot_tech_calc import (
    build_deterministic_tribunal,
    calc_indicators,
    suggest_levels,
    summarize_smc_and_structure,
)


def _now_utc_iso() -> str:
    return clock.now_iso()


def get_run_key(dt: Optional[datetime] = None) -> tuple[str, str]:
    if dt is None:
        dt = datetime.now(timezone.utc)
    # Map hour to run_time (closest)
    h = dt.hour
    if h < 9:
        rt = "07"
    elif h < 13:
        rt = "11"
    elif h < 16:
        rt = "14"
    else:
        rt = "18"
    run_key = dt.strftime("%Y-%m-%d") + "_" + rt
    return run_key, rt


class EurUsdSpotAnalysisService:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or paths.DB_PATH

    def _conn(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_latest(self) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM eurusd_spot_analysis_runs ORDER BY generated_at_utc DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)
        finally:
            conn.close()

    def get_history(self, limit: int = 50, run_time: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._conn()
        try:
            if run_time:
                cur = conn.execute(
                    "SELECT * FROM eurusd_spot_analysis_runs WHERE run_time=? ORDER BY generated_at_utc DESC LIMIT ?",
                    (run_time, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM eurusd_spot_analysis_runs ORDER BY generated_at_utc DESC LIMIT ?",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def get_today(self) -> List[Dict[str, Any]]:
        conn = self._conn()
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            cur = conn.execute(
                "SELECT * FROM eurusd_spot_analysis_runs WHERE run_key LIKE ? ORDER BY run_time",
                (today + "_%",),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def get_by_run_key(self, run_key: str) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT * FROM eurusd_spot_analysis_runs WHERE run_key=?", (run_key,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)
        finally:
            conn.close()

    def save_report(self, report: Dict[str, Any], sources_web: List[Dict[str, Any]]) -> int:
        validated = validate_report(report)
        c = count_consensus(validated.get("sources") or sources_web)

        price = validated.get("price") or {}
        meta = validated.get("meta") or {}
        run_key = meta["run_key"]
        run_time = meta["run_time"]
        generated_at_utc = meta["generated_at_utc"]

        conn = self._conn()
        try:
            existing_row = conn.execute(
                "SELECT id FROM eurusd_spot_analysis_runs WHERE run_key=?",
                (run_key,),
            ).fetchone()
            old_run_id = existing_row["id"] if existing_row else None

            cur = conn.execute(
                """INSERT OR REPLACE INTO eurusd_spot_analysis_runs
                (run_key, pair, run_time, generated_at_utc, bias, confidence,
                 price_current, change_pct_24h, high_24h, low_24h, price_source,
                 consensus_bull, consensus_bear, consensus_neutral, sources_count,
                 md_report, payload_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_key,
                    validated.get("meta", {}).get("pair") or "EURUSD",
                    run_time,
                    generated_at_utc,
                    validated["bias"],
                    int(validated["confidence"]),
                    price.get("current"),
                    price.get("change24h"),
                    price.get("high24h"),
                    price.get("low24h"),
                    price.get("source") or "",
                    c["bull"],
                    c["bear"],
                    c["neutral"],
                    len(validated.get("sources") or []),
                    validated.get("raw_summary_md") or "",
                    json.dumps(validated, ensure_ascii=False),
                ),
            )
            run_id = cur.lastrowid
            if old_run_id is not None and old_run_id != run_id:
                conn.execute("DELETE FROM eurusd_spot_analysis_sources WHERE run_id=?", (old_run_id,))
            conn.execute("DELETE FROM eurusd_spot_analysis_sources WHERE run_id=?", (run_id,))
            for s in validated.get("sources") or []:
                conn.execute(
                    """INSERT INTO eurusd_spot_analysis_sources
                    (run_id, name, domain, url, title, extracted_at_utc, sentiment)
                    VALUES (?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        s.get("name") or "",
                        s.get("domain") or "",
                        s.get("url") or "",
                        s.get("title") or "",
                        _now_utc_iso(),
                        s.get("sentiment") or "desconocido",
                    ),
                )
            conn.commit()
            return run_id
        finally:
            conn.close()

    def enrich_with_technical(
        self,
        closes: List[float],
        highs: List[float],
        lows: List[float],
        pdh: Optional[float] = None,
        pdl: Optional[float] = None,
    ) -> Dict[str, Any]:
        inds = calc_indicators(closes)
        lvls = suggest_levels(closes, highs, lows, pdh=pdh, pdl=pdl)
        return {"indicators": inds, "levels": lvls}

    def generate_analysis(
        self,
        market_service: Optional[Any] = None,
        force: bool = False,
        use_llm: bool = True,
        fetch_web: Optional[bool] = None,
        now: Optional[datetime] = None,
        web_collector: Optional[Callable[..., Dict[str, Any]]] = None,
        tribunal_runner: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        dt = now or datetime.now(timezone.utc)
        run_key, run_time = get_run_key(dt)
        do_fetch_web = use_llm if fetch_web is None else bool(fetch_web)

        if not force:
            existing = self.get_by_run_key(run_key)
            if existing:
                try:
                    payload = json.loads(existing.get("payload_json") or "{}")
                    if payload:
                        return payload
                except Exception:
                    pass

        # ------------------------------------------------------------------
        # 1. Evidencia Técnica y Microestructura SMC desde MetaTrader 5
        # ------------------------------------------------------------------
        last_known = self.get_latest()
        if last_known and last_known.get("price_current"):
            current_price = float(last_known["price_current"])
            price_source = f"Último precio guardado ({last_known.get('run_key', 'DB')})"
        else:
            current_price = 1.1150
            price_source = "FX Interbank / Estimación"

        bid_val: Optional[float] = None
        ask_val: Optional[float] = None
        spread_pts: Optional[float] = None
        change24h = -0.15
        high24h = round(current_price + 0.0035, 5)
        low24h = round(current_price - 0.0025, 5)
        closes: List[float] = []
        highs: List[float] = []
        lows: List[float] = []
        h1_closes: List[float] = []
        pdh: Optional[float] = None
        pdl: Optional[float] = None
        smc_analysis: Optional[Dict[str, Any]] = None

        if market_service is None:
            try:
                from api.services.market import MarketService
                market_service = MarketService()
            except Exception:
                pass

        if market_service is not None:
            try:
                p_data = market_service.price("EURUSD")
                if p_data:
                    if p_data.get("bid") is not None:
                        bid_val = round(float(p_data["bid"]), 5)
                    if p_data.get("ask") is not None:
                        ask_val = round(float(p_data["ask"]), 5)
                    current_price = bid_val if bid_val is not None else (ask_val or current_price)
                    price_source = "MetaTrader 5 Real-time Tick"
            except Exception:
                pass

            if hasattr(market_service, "spread_points"):
                try:
                    spread_pts = market_service.spread_points("EURUSD")
                except Exception:
                    spread_pts = None
            if spread_pts is None and bid_val is not None and ask_val is not None:
                spread_pts = round((ask_val - bid_val) * 100000, 1)

            # Intentar pattern_data primero (velas M15 + PDH/PDL + zonas SMC reales)
            candles: List[Dict[str, Any]] = []
            if hasattr(market_service, "pattern_data"):
                try:
                    pdata = market_service.pattern_data("EURUSD", "M15", bars=200)
                    if isinstance(pdata, dict):
                        candles = pdata.get("candles") or []
                        pdh = pdata.get("pdh")
                        pdl = pdata.get("pdl")
                        smc_analysis = pdata.get("analysis")
                except Exception:
                    candles = []

            if not candles and hasattr(market_service, "candles"):
                try:
                    candles = market_service.candles("EURUSD", "M15", bars=200) or []
                except Exception:
                    candles = []

            if candles and len(candles) >= 14:
                closes = [float(c["close"]) for c in candles]
                highs = [float(c["high"]) for c in candles]
                lows = [float(c["low"]) for c in candles]
                if price_source != "MetaTrader 5 Real-time Tick":
                    current_price = round(closes[-1], 5)
                    price_source = "MetaTrader 5 M15 Closes"

                lookback = min(len(candles), 96)
                high24h = round(max(highs[-lookback:]), 5)
                low24h = round(min(lows[-lookback:]), 5)
                c_open = float(candles[-lookback]["open"])
                if c_open > 0:
                    change24h = round(((current_price - c_open) / c_open) * 100, 2)

            # Velas H1 para confirmación multi-timeframe (M15 vs H1)
            if hasattr(market_service, "candles"):
                try:
                    h1_candles = market_service.candles("EURUSD", "H1", bars=120) or []
                    if len(h1_candles) >= 20:
                        h1_closes = [float(c["close"]) for c in h1_candles]
                except Exception:
                    h1_closes = []

        if closes:
            tech = self.enrich_with_technical(closes, highs, lows, pdh=pdh, pdl=pdl)
        else:
            tech = {
                "indicators": {
                    "sma20": round(current_price + 0.0008, 5),
                    "sma50": round(current_price + 0.0018, 5),
                    "sma100": round(current_price + 0.0032, 5),
                    "sma200": round(current_price + 0.0050, 5),
                    "rsi14": 42.5,
                    "price": current_price,
                    "above_sma50": False,
                    "above_sma100": False,
                    "macd": {"macd": -0.0005, "signal": None, "hist": None},
                },
                "levels": {
                    "supports": [round(current_price - 0.0015, 5), round(current_price - 0.0040, 5)],
                    "resistances": [round(current_price + 0.0020, 5), round(current_price + 0.0055, 5)],
                },
            }

        inds = tech.get("indicators") or {}
        h1_inds = calc_indicators(h1_closes) if h1_closes else {}
        smc_summary = summarize_smc_and_structure(
            current_price=current_price,
            m15_indicators=inds,
            h1_indicators=h1_inds,
            smc_analysis=smc_analysis,
            pdh=pdh,
            pdl=pdl,
            spread_points=spread_pts,
        )
        tech["h1_indicators"] = h1_inds
        tech["smc_summary"] = smc_summary

        # ------------------------------------------------------------------
        # 2. Evidencia Web Viva (RSS Institucionales + Calendario Macro EUR/USD)
        # ------------------------------------------------------------------
        rss_items: List[Dict[str, Any]] = []
        calendar_events: List[Dict[str, Any]] = []
        if do_fetch_web:
            collector = web_collector or collect_live_web_context
            try:
                web_ctx = collector(now_utc=dt)
                rss_items = web_ctx.get("rss_items") or []
                calendar_events = web_ctx.get("calendar_events") or []
            except Exception:
                rss_items = []
                calendar_events = []

        det_mode = "rss_plus_deterministic" if (rss_items or calendar_events) else "deterministic_fallback"
        det_tribunal = build_deterministic_tribunal(
            current_price=current_price,
            m15_indicators=inds,
            levels=tech.get("levels") or {},
            smc_summary=smc_summary,
            rss_items=rss_items,
            calendar_events=calendar_events,
            grounding_mode=det_mode,
        )

        price_info = {
            "current": current_price,
            "bid": bid_val,
            "ask": ask_val,
            "spread_points": spread_pts,
            "change24h": change24h,
            "high24h": high24h,
            "low24h": low24h,
            "source": price_source,
        }

        # ------------------------------------------------------------------
        # 3. Debate del Tribunal en Gemini con Google Search Grounding
        # ------------------------------------------------------------------
        gemini_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        llm_res: Optional[Dict[str, Any]] = None
        if gemini_key and use_llm:
            runner = tribunal_runner or run_gemini_grounded_tribunal
            try:
                llm_res = runner(
                    api_key=gemini_key,
                    now_iso=clock.now_iso(),
                    price_info=price_info,
                    m15_inds=inds,
                    h1_inds=h1_inds,
                    levels=tech.get("levels") or {},
                    smc_summary=smc_summary,
                    rss_items=rss_items,
                    calendar_events=calendar_events,
                )
            except Exception:
                llm_res = None

        if llm_res and isinstance(llm_res.get("parsed"), dict):
            parsed = llm_res["parsed"]
            citations = llm_res.get("citations") or []
            queries = llm_res.get("queries") or []
            g_mode = "gemini_google_search" if (citations or queries) else "rss_plus_gemini"

            raw_verdict = parsed.get("verdict") if isinstance(parsed.get("verdict"), dict) else {}
            raw_thesis = parsed.get("thesis") if isinstance(parsed.get("thesis"), dict) else {}
            raw_anti = parsed.get("antithesis") if isinstance(parsed.get("antithesis"), dict) else {}

            bias = str(raw_verdict.get("bias") or parsed.get("bias") or det_tribunal["verdict"]["bias"]).strip().lower()
            if bias not in ("alcista", "bajista", "neutral"):
                bias = det_tribunal["verdict"]["bias"]

            raw_conf = raw_verdict.get("confidence", parsed.get("confidence", det_tribunal["verdict"]["confidence"]))
            try:
                confidence = max(0, min(100, int(round(float(raw_conf)))))
            except Exception:
                confidence = int(det_tribunal["verdict"]["confidence"])

            tribunal = {
                "grounding_mode": g_mode,
                "search_queries": queries,
                "calendar_events_count": len(calendar_events),
                "rss_headlines_count": len(rss_items),
                "thesis": {
                    "role": "Fiscal Técnico (MT5 + SMC)",
                    "direction": raw_thesis.get("direction") or det_tribunal["thesis"]["direction"],
                    "headline": raw_thesis.get("headline") or det_tribunal["thesis"]["headline"],
                    "points": raw_thesis.get("points") or det_tribunal["thesis"]["points"],
                    "smc_summary": smc_summary.get("summary_text", ""),
                },
                "antithesis": {
                    "role": "Abogado del Diablo (Web + Macro + Riesgo)",
                    "headline": raw_anti.get("headline") or det_tribunal["antithesis"]["headline"],
                    "points": raw_anti.get("points") or det_tribunal["antithesis"]["points"],
                    "macro_threats": raw_anti.get("macro_threats") or det_tribunal["antithesis"]["macro_threats"],
                },
                "verdict": {
                    "role": "Juez Cuantitativo",
                    "status": raw_verdict.get("status") or det_tribunal["verdict"]["status"],
                    "bias": bias,
                    "confidence": confidence,
                    "recommended_risk_pct": raw_verdict.get(
                        "recommended_risk_pct", det_tribunal["verdict"]["recommended_risk_pct"]
                    ),
                    "ruling": raw_verdict.get("ruling") or det_tribunal["verdict"]["ruling"],
                    "invalidation_condition": raw_verdict.get("invalidation_condition")
                    or det_tribunal["verdict"]["invalidation_condition"],
                },
            }

            fund_raw = parsed.get("fundamental") or []
            fund_points = (
                [{"text": str(x)} for x in fund_raw[:6] if str(x).strip()]
                if isinstance(fund_raw, list) and fund_raw
                else [{"text": p} for p in det_tribunal["thesis"]["points"]]
            )
            catalysts = (
                [str(x) for x in parsed["catalysts_today"][:6] if str(x).strip()]
                if isinstance(parsed.get("catalysts_today"), list) and parsed.get("catalysts_today")
                else self._build_default_catalysts(calendar_events, rss_items)
            )
            risks = (
                [str(x) for x in parsed["risks"][:6] if str(x).strip()]
                if isinstance(parsed.get("risks"), list) and parsed.get("risks")
                else list(det_tribunal["antithesis"]["points"])
            )
            c_vs_d = str(parsed.get("consensus_vs_dissent") or tribunal["verdict"]["ruling"])
            s_text = str(parsed.get("raw_summary_md") or "").strip()
            if not s_text:
                s_text = self._render_tribunal_markdown(price_info, tech, tribunal, calendar_events)

            llm_sources = parsed.get("sources_evaluated") if isinstance(parsed.get("sources_evaluated"), list) else []
            sources = merge_grounded_sources(rss_items, citations, llm_sources, default_bias=bias)
        else:
            tribunal = det_tribunal
            bias = tribunal["verdict"]["bias"]
            confidence = int(tribunal["verdict"]["confidence"])
            sources = merge_grounded_sources(rss_items, [], [], default_bias=bias)
            catalysts = self._build_default_catalysts(calendar_events, rss_items)
            risks = list(tribunal["antithesis"]["points"])
            fund_points = self._build_fallback_fundamentals(current_price, bias, inds, rss_items, calendar_events)
            c_vs_d = (
                f"{tribunal['verdict']['ruling']} | Invalidación: {tribunal['verdict']['invalidation_condition']}"
            )
            s_text = self._render_tribunal_markdown(price_info, tech, tribunal, calendar_events)

        report = {
            "meta": {
                "pair": "EURUSD",
                "generated_at_utc": clock.now_iso(),
                "run_key": run_key,
                "run_time": run_time,
                "timezone": "UTC",
                "disclaimer": "Esto no es asesoramiento financiero. Inteligencia consultiva para FTMO Copilot (confirmación humana obligatoria).",
            },
            "price": price_info,
            "bias": bias,
            "confidence": confidence,
            "tribunal": tribunal,
            "calendar_today": calendar_events,
            "fundamental": fund_points,
            "technical": tech,
            "consensus_vs_dissent": {"note": c_vs_d},
            "catalysts_today": catalysts,
            "risks": risks,
            "sources": sources,
            "raw_summary_md": s_text,
        }

        validated = validate_report(report)
        self.save_report(validated, validated["sources"])
        return validated

    @staticmethod
    def _build_default_catalysts(
        calendar_events: List[Dict[str, Any]],
        rss_items: List[Dict[str, Any]],
    ) -> List[str]:
        out: List[str] = []
        for ev in calendar_events[:4]:
            out.append(
                f"[{ev.get('time_utc')}] {ev.get('currency')} ({ev.get('impact')}): {ev.get('title')} "
                f"— Est: {ev.get('forecast') or 'n/d'} | Prev: {ev.get('previous') or 'n/d'}"
            )
        for it in rss_items[:2]:
            out.append(f"[{it.get('domain')}] {it.get('title')}")
        if not out:
            out = [
                "Ventanas operativas FTMO: Killzone Londres (07:00–10:00 UTC) y Nueva York (12:30–15:30 UTC).",
                "Diferencial de rendimientos soberanos a 10 años (US10Y Treasury vs. Bund alemán).",
                "Flujo institucional del índice DXY y posicionamiento en pares europeos.",
            ]
        return out[:6]

    @staticmethod
    def _build_fallback_fundamentals(
        current_price: float,
        bias: str,
        inds: Dict[str, Any],
        rss_items: List[Dict[str, Any]],
        calendar_events: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        points: List[Dict[str, str]] = []
        rsi_val = inds.get("rsi14") if inds.get("rsi14") is not None else 50.0
        points.append({
            "text": f"Estructura cuantitativa EUR/USD en {current_price} con sesgo {bias} (RSI14 M15: {float(rsi_val):.1f})."
        })
        for it in rss_items[:3]:
            points.append({"text": f"[{it.get('domain')}] {it.get('title')}"})
        if calendar_events:
            ev0 = calendar_events[0]
            points.append({
                "text": f"Agenda macro activa ({len(calendar_events)} eventos EUR/USD): próximo {ev0.get('currency')} "
                f"{ev0.get('title')} a las {ev0.get('time_utc')} ({ev0.get('impact')})."
            })
        if len(points) < 3:
            points.append({
                "text": "Divergencia de expectativas de política monetaria entre la Reserva Federal (FOMC) y el BCE."
            })
            points.append({
                "text": "Sensibilidad intradía a subastas de deuda y liquidez del dólar estadounidense (DXY)."
            })
        return points[:5]

    @staticmethod
    def _render_tribunal_markdown(
        price_info: Dict[str, Any],
        tech: Dict[str, Any],
        tribunal: Dict[str, Any],
        calendar_events: List[Dict[str, Any]],
    ) -> str:
        verdict = tribunal.get("verdict") or {}
        thesis = tribunal.get("thesis") or {}
        anti = tribunal.get("antithesis") or {}
        supports = ", ".join(str(s) for s in (tech.get("levels") or {}).get("supports") or []) or "N/A"
        resistances = ", ".join(str(r) for r in (tech.get("levels") or {}).get("resistances") or []) or "N/A"
        smc_text = (tech.get("smc_summary") or {}).get("summary_text") or "N/A"

        lines = [
            "# ⚖️ Tribunal Tripartito EUR/USD Spot",
            "",
            f"- **Precio MT5:** `{price_info.get('current')}` ({price_info.get('source')}) · **Cambio 24h:** `{price_info.get('change24h')}%`",
            f"- **Veredicto del Juez:** **{verdict.get('status')}** · Sesgo **{str(verdict.get('bias', '')).upper()}** "
            f"(Confianza `{verdict.get('confidence')}%`) · Riesgo sugerido: **`{verdict.get('recommended_risk_pct')}%`**",
            f"- **Modo de Datos Web:** `{tribunal.get('grounding_mode')}`",
            "",
            f"### ⚔️ Fiscal Técnico — Tesis ({thesis.get('direction')})",
            f"**{thesis.get('headline')}**",
        ]
        for pt in thesis.get("points") or []:
            lines.append(f"- {pt}")
        lines.extend([
            f"- *Microestructura SMC:* {smc_text}",
            "",
            "### 🛡️ Abogado del Diablo — Antítesis (Web & Macro)",
            f"**{anti.get('headline')}**",
        ])
        for pt in anti.get("points") or []:
            lines.append(f"- {pt}")
        lines.extend([
            "",
            "### ⚖️ Dictamen del Juez Cuantitativo",
            f"{verdict.get('ruling')}",
            f"- **Condición de Invalidación:** {verdict.get('invalidation_condition')}",
            "",
            "### 🎯 Niveles Técnicos Clave",
            f"- **Soportes:** {supports}",
            f"- **Resistencias:** {resistances}",
        ])
        if calendar_events:
            lines.extend(["", "### 📅 Agenda Macro Hoy (EUR / USD)"])
            for ev in calendar_events[:5]:
                lines.append(
                    f"- `{ev.get('time_utc')}` **{ev.get('currency')}** [{ev.get('impact')}] {ev.get('title')} "
                    f"(Est: {ev.get('forecast') or '-'}, Prev: {ev.get('previous') or '-'})"
                )
        return "\n".join(lines)
