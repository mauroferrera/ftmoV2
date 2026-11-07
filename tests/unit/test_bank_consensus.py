"""Tests unitarios para el módulo de Consenso Institucional Bancario (Meta-Análisis)."""
from __future__ import annotations

import json
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import analysis
from core.bank_consensus import (
    compute_consensus_metrics,
    format_bank_consensus_for_prompt,
    get_default_bank_analyses,
    load_bank_analyses_data,
)
from core.eurusd_spot_analysis_schema import validate_report


def test_default_bank_analyses_and_metrics():
    analyses = get_default_bank_analyses()
    assert len(analyses) == 10

    metrics = compute_consensus_metrics(analyses)
    assert metrics["total_items"] == 10
    assert metrics["bearish_count"] == 6
    assert metrics["neutral_count"] == 3
    assert metrics["bullish_count"] == 1
    assert metrics["bearish_pct"] == 60.0
    assert metrics["neutral_pct"] == 30.0
    assert metrics["bullish_pct"] == 10.0
    assert metrics["dominant_bias"] == "bearish"
    assert "1.1280" in metrics["institutional_resistance_ceiling"]
    assert "1.1220" in metrics["bank_fade_sell_zone"]
    assert "1.1295" in metrics["stops_invalidation_cluster"]
    assert "1.1100" in metrics["liquidity_target_magnet"]


def test_load_bank_analyses_data_fallback(tmp_path):
    # Sin archivo existente, carga el default en memoria
    data = load_bank_analyses_data(tmp_path / "non_existent.json")
    assert "metadata" in data
    assert "analyses" in data
    assert len(data["analyses"]) == 10

    # Con archivo válido
    custom_file = tmp_path / "custom.json"
    custom_payload = {
        "analyses": [
            {"id": 1, "institution": "Test Bank", "bias": "bullish", "thesis_summary": "Test thesis"}
        ]
    }
    custom_file.write_text(json.dumps(custom_payload), encoding="utf-8")
    loaded = load_bank_analyses_data(custom_file)
    assert len(loaded["analyses"]) == 1
    assert loaded["analyses"][0]["institution"] == "Test Bank"


def test_format_bank_consensus_for_prompt():
    data = load_bank_analyses_data()
    prompt_text = format_bank_consensus_for_prompt(data)
    assert "META-ANÁLISIS INSTITUCIONAL: CONSENSO DE BANCOS" in prompt_text
    assert "60.0% Bajista" in prompt_text
    assert "ING Think FX" in prompt_text
    assert "1.1280 - 1.1290" in prompt_text


def test_schema_validates_bank_consensus():
    base_payload = {
        "meta": {
            "pair": "EURUSD",
            "generated_at_utc": "2026-11-04T10:00:00Z",
            "run_key": "2026-11-04_11",
            "run_time": "11",
        },
        "bias": "bajista",
        "confidence": 65,
        "sources": [{"url": "https://example.com/eurusd", "sentiment": "bajista"}],
        "bank_consensus": load_bank_analyses_data(),
    }
    validated = validate_report(base_payload)
    assert "bank_consensus" in validated
    assert validated["bank_consensus"] is not None
    assert len(validated["bank_consensus"]["analyses"]) == 10


def test_bank_consensus_api_route():
    app = FastAPI()
    app.include_router(analysis.router)
    client = TestClient(app)

    res = client.get("/api/analysis/eurusd-spot/bank-consensus")
    assert res.status_code == 200
    data = res.json()
    assert "metadata" in data
    assert "analyses" in data
    assert len(data["analyses"]) == 10
    assert data["metadata"]["consensus"]["bearish_pct"] == 60.0

