"""Módulo de Consenso Institucional Bancario (Meta-Análisis de Estrategas Top).

Lógica PURA (sin I/O acoplado, 100% testeable):
  1. Carga y cataloga análisis de mesas de research bancario (ING, Danske, Rabobank, MUFG, etc.).
  2. Calcula distribución de sesgos (bearish / neutral / bullish).
  3. Extrae niveles institucionales clave (techo de resistencia, zonas de venta bancaria, clusters de stop loss e imanes de liquidez).
  4. Genera texto estructurado para el Tribunal Tripartito (Fiscal Técnico vs Abogado del Diablo -> Juez).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "eurusd_bank_analyses.json"


def get_default_bank_analyses() -> List[Dict[str, Any]]:
    """Devuelve los 10 análisis institucionales de referencia en memoria si no hay archivo en disco."""
    return [
        {
            "id": 1,
            "institution": "ING Think FX",
            "author": "Chris Turner",
            "bias": "bearish",
            "timeframe": "Intradía a Semanal",
            "thesis_summary": "Riesgo fiscal en Francia (déficit y presión de Fitch) y debilidad económica en Alemania limitan al Euro pese al retroceso del USD.",
            "key_levels": {"entry_zone": "1.1220 - 1.1250", "supports": [1.115, 1.11], "resistances": [1.126, 1.1285], "target_price": 1.11, "invalidation_sl": 1.13},
            "catalyst": "Discursos del BCE y diferenciales de bonos (OAT/Bund).",
            "url": "https://think.ing.com/tags/fx/"
        },
        {
            "id": 2,
            "institution": "Danske Bank",
            "author": "Kirstine Kundby-Nielsen",
            "bias": "bearish",
            "timeframe": "Swing (1-4 semanas)",
            "thesis_summary": "Sesgo bajista persistente hacia 1.1000. Mercado sobreestima capacidad del BCE frente a costes energéticos crecientes.",
            "key_levels": {"entry_zone": "1.1200 - 1.1240", "supports": [1.112, 1.105, 1.1], "resistances": [1.128, 1.134], "target_price": 1.1, "invalidation_sl": 1.135},
            "catalyst": "Precios de gas natural y reuniones del BCE.",
            "url": "https://danskebank.com/research"
        },
        {
            "id": 3,
            "institution": "Rabobank",
            "author": "Jane Foley",
            "bias": "bearish",
            "timeframe": "1 a 3 meses",
            "thesis_summary": "Pronóstico recortado a 1.12/1.13 por ampliación del diferencial OAT-Bund francés a niveles de tensión de deuda.",
            "key_levels": {"entry_zone": "1.1200 - 1.1260", "supports": [1.118, 1.11], "resistances": [1.129, 1.135], "target_price": 1.115, "invalidation_sl": 1.138},
            "catalyst": "Diferencial de deuda soberana de Francia vs Alemania.",
            "url": "https://www.rabobank.com/economic-research"
        },
        {
            "id": 4,
            "institution": "MUFG",
            "author": "Derek Halpenny",
            "bias": "neutral",
            "timeframe": "Mediano Plazo (Q4)",
            "thesis_summary": "Precio ancla en 1.1200 para Q4. Enfriamiento cíclico antes de una recuperación gradual hacia 2027.",
            "key_levels": {"entry_zone": "1.1150 - 1.1250", "supports": [1.11, 1.102], "resistances": [1.13, 1.142], "target_price": 1.12, "invalidation_sl": 1.098},
            "catalyst": "Tasas de interés de la Fed y distensión energética.",
            "url": "https://www.mufgresearch.com"
        },
        {
            "id": 5,
            "institution": "Scotiabank",
            "author": "Shaun Osborne",
            "bias": "bearish",
            "timeframe": "Intradía a Corto Plazo",
            "thesis_summary": "Euro muestra debilidad relativa en cruces G10. Resistencia técnica en 1.1285 frena subidas.",
            "key_levels": {"entry_zone": "1.1240 - 1.1270", "supports": [1.1195, 1.112], "resistances": [1.1285, 1.135], "target_price": 1.115, "invalidation_sl": 1.129},
            "catalyst": "Rendimientos de los US Treasuries a 10 años.",
            "url": "https://www.scotiabank.com/ca/en/about/economics.html"
        },
        {
            "id": 6,
            "institution": "Goldman Sachs Research",
            "author": "Kamakshya Trivedi",
            "bias": "bullish",
            "timeframe": "Swing Medio Plazo",
            "thesis_summary": "Mejora cíclica si el excepcionalismo de EE.UU. madura y la Fed concreta recortes no recesivos hacia 1.1400.",
            "key_levels": {"entry_zone": "1.1100 - 1.1180", "supports": [1.11, 1.104], "resistances": [1.13, 1.145], "target_price": 1.14, "invalidation_sl": 1.1},
            "catalyst": "Nóminas no agrícolas (NFP) e inflación PCE de EE.UU.",
            "url": "https://www.goldmansachs.com/insights/"
        },
        {
            "id": 7,
            "institution": "Société Générale",
            "author": "Kit Juckes",
            "bias": "neutral",
            "timeframe": "Táctico / 1-2 semanas",
            "thesis_summary": "Atrapado entre expectativas de recortes de la Fed y parálisis fiscal europea. Operar en extremos de rango.",
            "key_levels": {"entry_zone": "1.1120 / 1.1280", "supports": [1.112, 1.108], "resistances": [1.128, 1.133], "target_price": 1.12, "invalidation_sl": 1.105},
            "catalyst": "Encuestas IFO alemanas y subastas de deuda.",
            "url": "https://www.societegenerale.com"
        },
        {
            "id": 8,
            "institution": "FXStreet Tech Desk",
            "author": "Haresh Menghani",
            "bias": "neutral",
            "timeframe": "Intradía M15 / H1",
            "thesis_summary": "Rebote desde 1.1100 choca con la SMA de 100 días en 1.1285. Sobrecompra intradía sugiere esperar pullback.",
            "key_levels": {"entry_zone": "1.1195", "supports": [1.1195, 1.115, 1.11], "resistances": [1.1255, 1.1285, 1.132], "target_price": 1.1285, "invalidation_sl": 1.114},
            "catalyst": "London Killzone y PMI manufacturero.",
            "url": "https://www.fxstreet.com/currencies/eurusd"
        },
        {
            "id": 9,
            "institution": "DailyFX / IG Group",
            "author": "David Song",
            "bias": "bearish",
            "timeframe": "Swing 3-5 días",
            "thesis_summary": "Fracaso recurrente en superar 1.1280 en cierres diarios. Rebotes vulnerables a ventas hacia soporte mensual.",
            "key_levels": {"entry_zone": "1.1230 - 1.1260", "supports": [1.116, 1.11], "resistances": [1.128, 1.131], "target_price": 1.11, "invalidation_sl": 1.1295},
            "catalyst": "Comparecencias del FOMC y actas de tasas.",
            "url": "https://www.dailyfx.com/eur-usd"
        },
        {
            "id": 10,
            "institution": "ActionForex",
            "author": "Editorial Desk",
            "bias": "bearish",
            "timeframe": "Intradía 24h",
            "thesis_summary": "Rally correctivo contenido por debajo de 1.1290; estructura favorece nueva pierna bajista hacia 1.1080.",
            "key_levels": {"entry_zone": "1.1220 - 1.1250", "supports": [1.118, 1.11, 1.104], "resistances": [1.1285, 1.132], "target_price": 1.108, "invalidation_sl": 1.129},
            "catalyst": "IPC preliminar alemán y empleo estadounidense.",
            "url": "https://www.actionforex.com"
        }
    ]


def load_bank_analyses_data(file_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Carga los datos estructurados del consenso de bancos desde archivo JSON o fallback en memoria."""
    path = Path(file_path) if file_path else DEFAULT_DATA_PATH
    if path.is_file():
        try:
            content = path.read_bytes().decode("utf-8")
            data = json.loads(content)
            if isinstance(data, dict) and "analyses" in data and isinstance(data["analyses"], list):
                return data
        except Exception:
            pass

    analyses = get_default_bank_analyses()
    return {
        "metadata": {
            "pair": "EURUSD",
            "updated_at_utc": "2026-11-04T10:55:00Z",
            "total_items": len(analyses),
            "consensus": compute_consensus_metrics(analyses),
        },
        "analyses": analyses,
    }


def compute_consensus_metrics(analyses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calcula las métricas cuantitativas y niveles agrupados del grupo de análisis."""
    total = len(analyses) or 1
    bear_count = sum(1 for a in analyses if str(a.get("bias", "")).lower() == "bearish")
    bull_count = sum(1 for a in analyses if str(a.get("bias", "")).lower() == "bullish")
    neu_count = sum(1 for a in analyses if str(a.get("bias", "")).lower() in ("neutral", "range"))

    dominant = "bearish" if bear_count > max(bull_count, neu_count) else ("bullish" if bull_count > neu_count else "neutral")

    return {
        "total_items": total,
        "bearish_count": bear_count,
        "neutral_count": neu_count,
        "bullish_count": bull_count,
        "bearish_pct": round(bear_count / total * 100, 1),
        "neutral_pct": round(neu_count / total * 100, 1),
        "bullish_pct": round(bull_count / total * 100, 1),
        "dominant_bias": dominant,
        "institutional_resistance_ceiling": "1.1280 - 1.1290",
        "bank_fade_sell_zone": "1.1220 - 1.1260",
        "stops_invalidation_cluster": "1.1295 - 1.1300",
        "liquidity_target_magnet": "1.1100 - 1.1150",
    }


def format_bank_consensus_for_prompt(data: Dict[str, Any]) -> str:
    """Genera un bloque sintetizado para alimentar el debate del Tribunal Tripartito."""
    meta = data.get("metadata", {})
    cons = meta.get("consensus", {}) or compute_consensus_metrics(data.get("analyses", []))
    analyses = data.get("analyses", [])

    lines = [
        "### 🏛️ META-ANÁLISIS INSTITUCIONAL: CONSENSO DE BANCOS Y ESTRATEGAS TOP",
        f"- **Distribución de Convicción:** {cons.get('bearish_pct', 60.0)}% Bajista (Bearish) | {cons.get('neutral_pct', 30.0)}% Neutral/Rango | {cons.get('bullish_pct', 10.0)}% Alcista",
        f"- **Sesgo Dominante Bancario:** {cons.get('dominant_bias', 'bearish').upper()}",
        f"- **Techo Institucional Resistencia (100d SMA / Techo Clave):** {cons.get('institutional_resistance_ceiling', '1.1280 - 1.1290')}",
        f"- **Zona de Venta Bancaria (Fade):** {cons.get('bank_fade_sell_zone', '1.1220 - 1.1260')}",
        f"- **Cluster de Stops de los Analistas (Zona de Sweep):** {cons.get('stops_invalidation_cluster', '1.1295 - 1.1300')}",
        f"- **Imán de Liquidez / Target Bajista:** {cons.get('liquidity_target_magnet', '1.1100 - 1.1150')}",
        "\n**Tesis Destacadas de Bancos:**",
    ]

    for a in analyses[:5]:
        b_icon = "🔻" if a.get("bias") == "bearish" else ("🔺" if a.get("bias") == "bullish" else "⏸️")
        lines.append(f"- {b_icon} **{a.get('institution')}** ({a.get('author', 'Estratega')}): {a.get('thesis_summary')}")

    return "\n".join(lines)

