"""Tests unitarios y de rutas para Inteligencia EUR/USD Spot y Tribunal Tripartito.

Cubre:
  - core/eurusd_spot_analysis_schema.py: validate_report, validate_tribunal, count_consensus, retrocompatibilidad
  - core/eurusd_spot_tech_calc.py: sma, ema, rsi, macd (con signal/hist), summarize_smc_and_structure, build_deterministic_tribunal
  - api/services/eurusd_web_intel.py: parse_rss_xml, parse_calendar_json, extract_json_object, run_gemini_grounded_tribunal, merge_grounded_sources
  - api/services/eurusd_spot_analysis_service.py: generate_analysis (fallback y grounded), save_report, get_latest, get_history
  - api/services/setup_eval.py: macro_confluence enriquecida con el veredicto del Tribunal en EURUSD
  - api/routes/analysis.py: POST /api/analysis/eurusd-spot/run aislado en tmp_path sin exigir API_TOKEN
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Dict

import pytest

from api.services.eurusd_spot_analysis_service import (
    EurUsdSpotAnalysisService,
    get_run_key,
)
from api.services.eurusd_web_intel import (
    extract_domain,
    extract_json_object,
    merge_grounded_sources,
    parse_calendar_json,
    parse_rss_xml,
    run_gemini_grounded_tribunal,
)
from core.eurusd_spot_analysis_schema import (
    EurUsdSpotAnalysisError,
    count_consensus,
    validate_report,
    validate_tribunal,
)
from core.eurusd_spot_tech_calc import (
    build_deterministic_tribunal,
    calc_indicators,
    ema,
    macd,
    rsi,
    sma,
    suggest_levels,
    summarize_smc_and_structure,
)
from database.models import init_schema


def test_schema_validations():
    with pytest.raises(EurUsdSpotAnalysisError, match="payload debe ser dict"):
        validate_report("invalido")  # type: ignore[arg-type]

    with pytest.raises(EurUsdSpotAnalysisError, match="meta debe ser dict"):
        validate_report({"meta": "string"})

    with pytest.raises(EurUsdSpotAnalysisError, match="bias inválido"):
        validate_report({
            "meta": {"pair": "EURUSD", "generated_at_utc": "2026-10-28T12:00:00Z", "run_key": "2026-10-28_11", "run_time": "11"},
            "bias": "invalido",
            "confidence": 50,
            "sources": [{"url": "https://example.com"}],
        })

    with pytest.raises(EurUsdSpotAnalysisError, match="confidence debe ser int"):
        validate_report({
            "meta": {"pair": "EURUSD", "generated_at_utc": "2026-10-28T12:00:00Z", "run_key": "2026-10-28_11", "run_time": "11"},
            "bias": "alcista",
            "confidence": 150,
            "sources": [{"url": "https://example.com"}],
        })

    with pytest.raises(EurUsdSpotAnalysisError, match="se requiere al menos 1 fuente"):
        validate_report({
            "meta": {"pair": "EURUSD", "generated_at_utc": "2026-10-28T12:00:00Z", "run_key": "2026-10-28_11", "run_time": "11"},
            "bias": "alcista",
            "confidence": 70,
            "sources": [],
        })


def test_tribunal_schema_normalization_and_backwards_compat():
    # Payload legado sin `tribunal` ni `calendar_today` debe normalizarse sin romper
    legacy = validate_report({
        "meta": {"pair": "EURUSD", "generated_at_utc": "2026-10-29T12:00:00Z", "run_key": "2026-10-29_11", "run_time": "11"},
        "bias": "alcista",
        "confidence": 72,
        "sources": [
            {"url": "https://www.ecb.europa.eu/press/1", "sentiment": "alcista"},
            {"url": "https://www.ecb.europa.eu/press/1", "sentiment": "alcista"},  # duplicada
        ],
    })
    assert len(legacy["sources"]) == 1
    assert legacy["tribunal"]["thesis"]["direction"] == "BUY"
    assert legacy["tribunal"]["verdict"]["bias"] == "alcista"
    assert legacy["tribunal"]["verdict"]["confidence"] == 72
    assert legacy["calendar_today"] == []

    # Payload explícito del Tribunal
    trib = validate_tribunal({
        "grounding_mode": "gemini_google_search",
        "search_queries": ["EURUSD ECB Fed news today"],
        "calendar_events_count": 2,
        "rss_headlines_count": 4,
        "thesis": {"direction": "SELL", "headline": "Caída bajo OB", "points": ["OB en 1.1180"]},
        "antithesis": {"headline": "NFP hoy", "points": ["Riesgo NFP 12:30 UTC"]},
        "verdict": {
            "status": "VETO",
            "bias": "bajista",
            "confidence": 64,
            "recommended_risk_pct": 0.0,
            "ruling": "Esperar NFP",
            "invalidation_condition": "Cierre M15 > 1.1185",
        },
    })
    assert trib["grounding_mode"] == "gemini_google_search"
    assert trib["verdict"]["status"] == "VETO"
    assert trib["verdict"]["recommended_risk_pct"] == 0.0
    assert trib["search_queries"] == ["EURUSD ECB Fed news today"]


def test_count_consensus():
    sources = [
        {"sentiment": "alcista"},
        {"sentiment": "alcista"},
        {"sentiment": "bajista"},
        {"sentiment": "neutral"},
        {"sentiment": "desconocido"},
    ]
    res = count_consensus(sources)
    assert res["bull"] == 2
    assert res["bear"] == 1
    assert res["neutral"] == 1


def test_technical_calculations():
    closes = [1.1000 + i * 0.0005 for i in range(60)]
    s20 = sma(closes, 20)
    assert s20 is not None
    assert round(s20, 4) == round(sum(closes[-20:]) / 20, 4)

    e10 = ema(closes, 10)
    assert e10 is not None

    r = rsi(closes, 14)
    assert r is not None
    assert 0 <= r <= 100

    m = macd(closes)
    assert m["macd"] is not None
    assert m["signal"] is not None
    assert m["hist"] is not None

    inds = calc_indicators(closes)
    assert "sma20" in inds
    assert "rsi14" in inds
    assert inds["price"] == closes[-1]

    highs = [c + 0.0005 for c in closes]
    lows = [c - 0.0005 for c in closes]
    lvls = suggest_levels(closes, highs, lows, pdh=closes[-1] + 0.0030, pdl=closes[-1] - 0.0040)
    assert len(lvls["supports"]) >= 1
    assert len(lvls["resistances"]) >= 1


def test_smc_structure_and_deterministic_tribunal():
    m15_inds = {"sma50": 1.1120, "sma100": 1.1100, "rsi14": 58.0, "above_sma50": True, "above_sma100": True, "macd": {"hist": 0.00012}}
    h1_inds = {"sma50": 1.1110, "rsi14": 56.0, "above_sma50": True}
    smc_analysis = {
        "patterns": {
            "fvgs": [{"type": "BULLISH_FVG", "bottom": 1.1140, "top": 1.1148}],
            "order_blocks": [{"type": "BULLISH_OB", "bottom": 1.1130, "top": 1.1138}],
            "sweeps": [{"type": "PDL_SWEEP", "price_level": 1.1105}],
        }
    }
    summary = summarize_smc_and_structure(
        current_price=1.1155,
        m15_indicators=m15_inds,
        h1_indicators=h1_inds,
        smc_analysis=smc_analysis,
        pdh=1.1190,
        pdl=1.1105,
        spread_points=4.0,
    )
    assert summary["m15_trend"] == "alcista"
    assert summary["h1_trend"] == "alcista"
    assert summary["mtf_aligned"] is True
    assert summary["bull_fvgs_count"] == 1
    assert summary["bull_obs_count"] == 1
    assert "PDL" in (summary["recent_sweep"] or "")

    levels = {"supports": [1.1135, 1.1105], "resistances": [1.1190]}

    # Caso 1: Alta confluencia sin eventos inminentes
    t_alta = build_deterministic_tribunal(1.1155, m15_inds, levels, summary)
    assert t_alta["thesis"]["direction"] == "BUY"
    assert t_alta["verdict"]["status"] == "ALTA_CONFLUENCIA"
    assert t_alta["verdict"]["recommended_risk_pct"] == 0.50

    # Caso 2: Evento High-Impact inminente dispara VETO del Juez
    imminent_cal = [{"time_utc": "12:30 UTC", "currency": "USD", "impact": "High", "title": "Core CPI m/m", "minutes_to_event": 20}]
    t_veto = build_deterministic_tribunal(1.1155, m15_inds, levels, summary, calendar_events=imminent_cal)
    assert t_veto["verdict"]["status"] == "VETO"
    assert t_veto["verdict"]["recommended_risk_pct"] == 0.0

    # Caso 3: Divergencia M15 alcista vs H1 bajista reduce riesgo a 0.25%
    summary_conflict = dict(summary, h1_trend="bajista", mtf_aligned=False)
    t_red = build_deterministic_tribunal(1.1155, m15_inds, levels, summary_conflict)
    assert t_red["verdict"]["status"] == "RIESGO_REDUCIDO"
    assert t_red["verdict"]["recommended_risk_pct"] == 0.25


def test_rss_and_calendar_parsers():
    now = datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc)
    sample_rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>EUR/USD climbs above 1.1160 as dollar weakens ahead of Fed</title>
        <link>https://www.fxstreet.com/news/eur-usd-climbs-20261029</link>
        <pubDate>Thu, 29 Oct 2026 12:30:00 GMT</pubDate>
        <description>Euro strengthens against USD in London session.</description>
      </item>
      <item>
        <title>Unrelated crypto altcoin gaming token launch</title>
        <link>https://www.fxstreet.com/news/altcoin-20261029</link>
        <pubDate>Thu, 29 Oct 2026 12:00:00 GMT</pubDate>
      </item>
    </channel></rss>"""

    items = parse_rss_xml(
        sample_rss,
        {"name": "FXStreet", "domain": "fxstreet.com", "kind": "fx_wire"},
        now_utc=now,
    )
    assert len(items) == 1
    assert items[0]["sentiment"] == "alcista"
    assert items[0]["domain"] == "fxstreet.com"

    cal_raw = [
        {"title": "Unemployment Claims", "country": "USD", "date": "2026-10-29T14:30:00+00:00", "impact": "High", "forecast": "215K", "previous": "212K"},
        {"title": "BOJ Speech", "country": "JPY", "date": "2026-10-29T15:00:00+00:00", "impact": "High"},
    ]
    evs = parse_calendar_json(cal_raw, now_utc=now)
    assert len(evs) == 1
    assert evs[0]["currency"] == "USD"
    assert evs[0]["minutes_to_event"] == 30

    wrapped_json = "```json\n{\"bias\": \"alcista\", \"confidence\": 74}\n```"
    assert extract_json_object(wrapped_json) == {"bias": "alcista", "confidence": 74}
    assert extract_domain("https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc", "reuters.com") == "reuters.com"


def test_gemini_grounded_tribunal_mock_http():
    class FakeResp:
        status_code = 200

        def json(self) -> Dict[str, Any]:
            return {
                "steps": [
                    {"type": "google_search_call", "arguments": {"queries": ["EUR USD ECB Fed news today"]}},
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({
                                    "bias": "alcista",
                                    "confidence": 76,
                                    "thesis": {"direction": "BUY", "headline": "Impulso sobre FVG M15", "points": ["FVG 1.1150"]},
                                    "antithesis": {"headline": "Resistencia PDH cercana", "points": ["PDH en 1.1180"]},
                                    "verdict": {
                                        "status": "ALTA_CONFLUENCIA",
                                        "bias": "alcista",
                                        "confidence": 76,
                                        "recommended_risk_pct": 0.5,
                                        "ruling": "Gana el Fiscal por alineación M15/H1 y debilidad del DXY.",
                                        "invalidation_condition": "Cierre M15 < 1.1135",
                                    },
                                }),
                                "annotations": [
                                    {"type": "url_citation", "url": "https://www.reuters.com/markets/currencies/euro-2026", "title": "reuters.com"}
                                ],
                            }
                        ],
                    },
                ]
            }

    res = run_gemini_grounded_tribunal(
        api_key="fake-key",
        now_iso="2026-10-29T15:00:00Z",
        price_info={"current": 1.1163, "source": "MT5"},
        m15_inds={"rsi14": 57.0},
        h1_inds={"rsi14": 55.0},
        levels={"supports": [1.1140], "resistances": [1.1180]},
        smc_summary={"summary_text": "M15 ALCISTA | H1 ALCISTA"},
        rss_items=[],
        calendar_events=[],
        http_post=lambda *a, **kw: FakeResp(),
    )
    assert res is not None
    assert res["queries"] == ["EUR USD ECB Fed news today"]
    assert res["citations"][0]["domain"] == "reuters.com"
    assert res["parsed"]["verdict"]["status"] == "ALTA_CONFLUENCIA"

    merged = merge_grounded_sources([], res["citations"], [], default_bias="alcista")
    assert merged[0]["domain"] == "reuters.com"
    assert merged[0]["origin"] == "google_search"


class FakeMarketService:
    def __init__(self, bid=1.1163, ask=1.1165):
        self.bid = bid
        self.ask = ask

    def price(self, symbol: str):
        return {"bid": self.bid, "ask": self.ask, "symbol": symbol}

    def spread_points(self, symbol: str):
        return 2.0

    def candles(self, symbol: str, timeframe: str = "M15", bars: int = 200):
        base = 1.1050
        out = []
        for i in range(bars):
            p = base + i * 0.00006
            out.append({
                "open": p - 0.00003,
                "high": p + 0.00012,
                "low": p - 0.00008,
                "close": p,
                "volume": 100,
                "time": 1700000000 + i * 900,
            })
        return out


def test_service_generation_and_persistence(tmp_path):
    db_path = str(tmp_path / "test_spot.db")
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.close()

    svc = EurUsdSpotAnalysisService(db_path=db_path)
    fake_market = FakeMarketService()

    # 1. Modo determinista offline (sin red)
    report = svc.generate_analysis(market_service=fake_market, force=True, use_llm=False)
    assert report["meta"]["pair"] == "EURUSD"
    assert report["bias"] in ("alcista", "bajista", "neutral")
    assert report["price"]["current"] == 1.1163
    assert report["tribunal"]["grounding_mode"] == "deterministic_fallback"
    assert report["tribunal"]["verdict"]["status"] in ("ALTA_CONFLUENCIA", "RIESGO_REDUCIDO", "VETO", "NEUTRAL")

    # 2. Modo Grounded con colector web e IA inyectados
    def fake_collector(now_utc=None):
        return {
            "rss_items": [{
                "name": "BCE",
                "domain": "ecb.europa.eu",
                "url": "https://www.ecb.europa.eu/press/pr/date/2026/html/pr1.en.html",
                "title": "ECB monetary policy statement",
                "published_utc": "2026-10-29T13:00:00Z",
                "sentiment": "alcista",
            }],
            "calendar_events": [{
                "time_utc": "18:00 UTC",
                "currency": "USD",
                "impact": "High",
                "title": "FOMC Member Speech",
                "forecast": "",
                "previous": "",
                "minutes_to_event": 180,
            }],
        }

    def fake_runner(**kwargs):
        return {
            "model": "gemini-3.8-flash",
            "queries": ["EUR/USD ECB Fed yields today"],
            "citations": [{
                "name": "fxstreet.com",
                "domain": "fxstreet.com",
                "url": "https://www.fxstreet.com/news/eurusd-live-today",
                "title": "EUR/USD holds gains above 1.1160",
                "origin": "google_search",
            }],
            "parsed": {
                "bias": "alcista",
                "confidence": 79,
                "thesis": {"direction": "BUY", "headline": "Tendencia M15+H1 alcista en MT5", "points": ["Precio 1.1163 > SMA50"]},
                "antithesis": {"headline": "Discurso FOMC a las 18:00 UTC", "points": ["Volatilidad vespertina USD"]},
                "verdict": {
                    "status": "ALTA_CONFLUENCIA",
                    "bias": "alcista",
                    "confidence": 79,
                    "recommended_risk_pct": 0.5,
                    "ruling": "Confluencia alcista confirmada antes del evento de las 18:00 UTC.",
                    "invalidation_condition": "Pérdida de 1.1140 en M15",
                },
                "fundamental": ["Diferencial Bund-Treasury estable", "BCE mantiene tono prudente"],
                "raw_summary_md": "# Resumen Tribunal\nDictamen: ALTA_CONFLUENCIA",
            },
        }

    # Forzar con API key simulada
    orig_key = svc
    import os
    old_env = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "test-key"
    try:
        grounded = svc.generate_analysis(
            market_service=fake_market,
            force=True,
            use_llm=True,
            fetch_web=True,
            web_collector=fake_collector,
            tribunal_runner=fake_runner,
        )
    finally:
        if old_env is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = old_env

    assert grounded["tribunal"]["grounding_mode"] == "gemini_google_search"
    assert grounded["tribunal"]["verdict"]["status"] == "ALTA_CONFLUENCIA"
    assert grounded["tribunal"]["verdict"]["recommended_risk_pct"] == 0.5
    assert len(grounded["calendar_today"]) == 1
    assert any(s["domain"] == "ecb.europa.eu" for s in grounded["sources"])
    assert any(s["domain"] == "fxstreet.com" for s in grounded["sources"])

    latest = svc.get_latest()
    assert latest is not None
    assert latest["bias"] == "alcista"
    assert latest["confidence"] == 79
    assert len(svc.get_history(limit=10)) == 1


def test_setup_eval_macro_confluence(tmp_path, monkeypatch):
    import api.services.eurusd_spot_analysis_service as spot_mod
    from api.services.setup_eval import evaluate

    db_path = str(tmp_path / "test_eval.db")
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.close()

    temp_svc = EurUsdSpotAnalysisService(db_path=db_path)
    temp_svc.generate_analysis(market_service=FakeMarketService(), force=True, use_llm=False)

    # Monkeypatch para que setup_eval lea nuestra DB aislada y nunca toque trading.db
    monkeypatch.setattr(spot_mod, "EurUsdSpotAnalysisService", lambda db_path=None: temp_svc)

    fake_snapshot = {
        "symbol": "EURUSD",
        "timeframe": "M15",
        "current_price": 1.1163,
        "last_time": 1700000000,
        "analysis": {
            "patterns": {
                "fvgs": [{"type": "BULLISH_FVG", "bottom": 1.1150, "top": 1.1158}],
                "order_blocks": [{"type": "BULLISH_OB", "bottom": 1.1140, "top": 1.1148}],
                "sweeps": [{"type": "PDL_SWEEP"}],
            }
        },
        "risk_engine": {
            "bull": {"score": 85.0, "verdict": "ALTA_PROBABILIDAD", "components": {"smc": {"value": 1.0}, "killzone": {"in_killzone": True, "name": "Londres"}}},
            "bear": {"score": 20.0, "verdict": "SIN_OPERATIVA", "components": {}},
            "invalidation": {"BUY": 1.1140, "SELL": None},
        },
    }
    cfg = {
        "score": {"min_score": 60.0},
        "execution": {"sl_distance_by_symbol": {"EURUSD": 0.0012}, "tp_ratio_r": 2.0},
    }

    res = evaluate(fake_snapshot, cfg=cfg)
    assert res["symbol"] == "EURUSD"
    assert res["approved"] is True
    assert "macro_confluence" in res
    mc = res["macro_confluence"]
    assert mc["pair"] == "EURUSD"
    assert mc["bias"] == "alcista"
    assert mc["aligned"] is True
    assert mc["verdict_status"] in ("ALTA_CONFLUENCIA", "RIESGO_REDUCIDO", "VETO", "NEUTRAL")
    assert mc["recommended_risk_pct"] in (0.5, 0.25, 0.0)


def test_analysis_run_route_does_not_require_token(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import analysis

    db_path = str(tmp_path / "test_route.db")
    conn = sqlite3.connect(db_path)
    init_schema(conn)
    conn.close()

    isolated_svc = EurUsdSpotAnalysisService(db_path=db_path)

    class OfflineIsolatedService(EurUsdSpotAnalysisService):
        def generate_analysis(self, market_service=None, force=False, **kwargs):
            return super().generate_analysis(
                market_service=FakeMarketService(),
                force=force,
                use_llm=False,
                fetch_web=False,
            )

    isolated_svc = OfflineIsolatedService(db_path=db_path)
    monkeypatch.setattr(analysis, "svc", isolated_svc)

    app = FastAPI()
    app.include_router(analysis.router)
    client = TestClient(app)

    # El endpoint /run no exige API_TOKEN y persiste en la BD aislada
    res = client.post("/api/analysis/eurusd-spot/run?force=true")
    assert res.status_code == 200
    data = res.json()
    assert data["meta"]["pair"] == "EURUSD"
    assert "tribunal" in data
    assert data["tribunal"]["verdict"]["status"] in ("ALTA_CONFLUENCIA", "RIESGO_REDUCIDO", "VETO", "NEUTRAL")
    assert "_meta_db" in data

    hist = client.get("/api/analysis/eurusd-spot/history")
    assert hist.status_code == 200
    assert hist.json()["count"] == 1
    assert "verdict_status" in hist.json()["items"][0]

    # Endpoint de Auditoría y Scorecard
    audit_res = client.get("/api/analysis/eurusd-spot/audit-metrics")
    assert audit_res.status_code == 200
    metrics = audit_res.json()
    assert "total_runs" in metrics
    assert "accuracy_4h" in metrics
    assert "web_consensus_accuracy_4h" in metrics
    assert "key_takeaways" in metrics
    assert len(metrics["key_takeaways"]) > 0
