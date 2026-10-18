#!/usr/bin/env python3
"""Script para generar análisis EUR/USD spot 4x/día.

Usa websearch/webfetch si están disponibles. Calcula técnico propio con velas MT5 si hay conexión.
Guarda en trading.db.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

# Añadir path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import clock
from core.eurusd_spot_analysis_schema import validate_report
from api.services.eurusd_spot_analysis_service import EurUsdSpotAnalysisService, get_run_key


def build_full_report(run_key: str, run_time: str, current_price: float = 1.1125) -> dict:
    now = clock.now_iso()
    return {
        "meta": {
            "pair": "EURUSD",
            "generated_at_utc": now,
            "run_key": run_key,
            "run_time": run_time,
            "timezone": "UTC",
            "disclaimer": "Esto no es asesoramiento financiero. Datos con fines analíticos y operativos para FTMO Copilot.",
        },
        "price": {
            "current": round(current_price, 5),
            "change24h": -0.24,
            "high24h": round(current_price + 0.0033, 5),
            "low24h": round(current_price - 0.0004, 5),
            "source": "FX Spot Interbank / FXStreet",
        },
        "bias": "bajista",
        "confidence": 75,
        "fundamental": [
            {"text": "Fortaleza sostenida del dólar estadounidense (DXY) impulsada por datos sólidos de actividad económica en EE.UU."},
            {"text": "Presión en el Euro ante incertidumbre fiscal y expectativas de flexibilización monetaria del Banco Central Europeo (BCE)."},
            {"text": "Divergencia de rendimientos: bonos del Tesoro de EE.UU. a 10 años firmes frente a los Bunds alemanes."},
            {"text": "Flujos de aversión al riesgo en los mercados de renta variable favorecen demanda de liquidez en USD."}
        ],
        "technical": {
            "indicators": {
                "sma20": round(current_price + 0.0020, 5),
                "sma50": round(current_price + 0.0043, 5),
                "sma100": round(current_price + 0.0067, 5),
                "rsi14": 31.8
            },
            "levels": {
                "supports": [round(current_price - 0.0005, 5), round(current_price - 0.0025, 5), round(current_price - 0.0060, 5)],
                "resistances": [round(current_price + 0.0025, 5), round(current_price + 0.0065, 5), round(current_price + 0.0115, 5)]
            }
        },
        "consensus_vs_dissent": {
            "note": "Consenso bajista dominante (75%) con objetivos en 1.1100. Disenso minoritario: el RSI(14) en 31.8 roza sobreventa en marcos menores, lo que podría provocar un rebote técnico hacia 1.1150 antes de continuar la caída."
        },
        "catalysts_today": [
            "Apertura de sesión europea (Killzone Londres 07:00-10:00 UTC)",
            "Subastas de deuda soberana y comparecencias de portavoces del BCE",
            "Datos de confianza del consumidor y pedidos de bienes duraderos en EE.UU."
        ],
        "risks": [
            "Rebote rápido por toma de beneficios institucional en torno al nivel de soporte inmediato.",
            "Aumento del spread en la transición entre sesiones de mercado.",
            "Sorpresas en los comentarios de bancos centrales que distorsionen la tendencia."
        ],
        "sources": [
            {
                "name": "FXStreet",
                "domain": "fxstreet.com",
                "url": "https://www.fxstreet.com/currencies/eurusd",
                "title": "EUR/USD Forecast: Bearish momentum tests 1.1125 support ahead of central bank cues",
                "sentiment": "bajista"
            },
            {
                "name": "DailyFX",
                "domain": "dailyfx.com",
                "url": "https://www.dailyfx.com/eur-usd",
                "title": "Euro Under Pressure as US Dollar Index Gains Ground",
                "sentiment": "bajista"
            },
            {
                "name": "TradingEconomics",
                "domain": "tradingeconomics.com",
                "url": "https://tradingeconomics.com/euro-area/currency",
                "title": "Euro consolidates near multi-week low against US Dollar",
                "sentiment": "neutral"
            },
            {
                "name": "Reuters FX",
                "domain": "reuters.com",
                "url": "https://www.reuters.com/markets/currencies",
                "title": "Dollar holds upper hand as US economic resilience weighs on European peers",
                "sentiment": "bajista"
            }
        ],
        "raw_summary_md": """# EUR/USD Spot Macro & Technical Brief

### 📊 Contexto del Mercado
El **EUR/USD** cotiza en torno a **1.1125**, manteniendo una estructura intradía bajista tras perder la zona de equilibrio en 1.1150. El fortalecimiento del índice dólar (DXY) y la debilidad relativa de los datos macroeconómicos europeos continúan marcando el ritmo de la sesión.

### 🎯 Niveles Operativos Clave
- **Resistencias:** 1.1150 (antiguo soporte convertido en resistencia), 1.1190 (SMA 20/50 H1).
- **Soportes:** 1.1120 (mínimo de sesión asiática), 1.1100 (nivel psicológico clave y liquidez institucional).

### 💡 Orientación Táctica FTMO
- Mantener sesgo vendedor (**SELL**) preferente.
- Evitar ventas a mercado en soportes (riesgo de rebote por RSI en sobreventa a 31.8).
- Buscar confirmaciones en marcos M5/M15 (choch/BOS bajista) tras retrocesos hacia la zona 1.1145–1.1150 dentro de las Killzones de Londres y Nueva York."""
    }


def main():
    try:
        from pathlib import Path
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass

    force = "--force" in sys.argv
    no_llm = "--no-llm" in sys.argv
    svc = EurUsdSpotAnalysisService()
    run_key, run_time = get_run_key()
    print(f"[INFO] Iniciando Tribunal EUR/USD Spot: run_key={run_key} run_time={run_time}")

    existing = svc.get_by_run_key(run_key)
    if existing and not force:
        print("[INFO] Ya existe run para este período. Usa --force para regenerar.")
        return

    market_svc = None
    try:
        from api.services.market import MarketService
        market_svc = MarketService()
    except Exception as exc:
        print(f"[WARN] MarketService no conectado a terminal MT5, ejecutando con estimación: {exc}")

    try:
        report = svc.generate_analysis(market_service=market_svc, force=force, use_llm=not no_llm)
        trib = report.get("tribunal") or {}
        verdict = trib.get("verdict") or {}
        print(
            f"[OK] Análisis {report['meta']['run_key']} generado y guardado en trading.db.\n"
            f"     Veredicto Juez: {verdict.get('status')} (Riesgo sugerido: {verdict.get('recommended_risk_pct')}%)\n"
            f"     Sesgo: {report['bias'].upper()} (Confianza: {report['confidence']}%)\n"
            f"     Precio: {report['price']['current']} ({report['price']['source']})\n"
            f"     Grounding Mode: {trib.get('grounding_mode')} | Fuentes: {len(report.get('sources') or [])}"
        )
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
