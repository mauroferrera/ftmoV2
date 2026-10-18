"""
Detector de Huella Institucional (Ballenas) en TradingView
---------------------------------------------------------
Analiza:
1. Volumen real de contratos en Futuros CME (6E1! para EUR, GC1! para Oro).
2. Detección de Absorción y Clímax de Volumen (Wick Rejection + Pico de contratos).
3. Validación de Rupturas (Volumen genuino vs Fakeout / Trampa de liquidez).
4. Divergencia SMT macro entre EUR/USD y el Índice Dólar (TVC:DXY).
"""

import asyncio
import datetime
import json
import random
import re
import string
import sys
from typing import Any, Dict, List, Optional, Tuple

import websockets

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _format_ws_msg(func: str, param_list: list) -> str:
    payload = json.dumps({"m": func, "p": param_list}, separators=(",", ":"))
    return f"~m~{len(payload)}~m~{payload}"


async def fetch_tv_candles(
    symbol: str, timeframe: str = "15", n_bars: int = 30, timeout_sec: float = 8.0
) -> List[Dict[str, Any]]:
    """Descarga velas OHLCV desde el WebSocket de TradingView."""
    uri = "wss://data.tradingview.com/socket.io/websocket"
    session_id = f"cs_{''.join(random.choices(string.ascii_lowercase + string.digits, k=10))}"

    async with websockets.connect(
        uri,
        origin="https://www.tradingview.com",
        user_agent_header="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        open_timeout=timeout_sec,
    ) as ws:
        await ws.send(_format_ws_msg("set_auth_token", ["unauthorized_user_token"]))
        await ws.send(_format_ws_msg("chart_create_session", [session_id, ""]))

        sym_spec = json.dumps({"adjustment": "splits", "symbol": symbol})
        await ws.send(_format_ws_msg("resolve_symbol", [session_id, "sds_sym", "=" + sym_spec]))
        await ws.send(
            _format_ws_msg(
                "create_series", [session_id, "sds_1", "s1", "sds_sym", str(timeframe), n_bars, ""]
            )
        )

        deadline = asyncio.get_event_loop().time() + timeout_sec
        candles_out: List[Dict[str, Any]] = []

        while asyncio.get_event_loop().time() < deadline:
            try:
                res = await asyncio.wait_for(ws.recv(), timeout=2.5)
            except asyncio.TimeoutError:
                break

            for part in re.split(r"~m~\d+~m~", res):
                if not part:
                    continue
                if part.startswith("~h~"):
                    await ws.send(f"~m~{len(part)}~m~{part}")
                    continue
                try:
                    payload = json.loads(part)
                    if payload.get("m") == "timescale_update":
                        series_map = payload.get("p", [None, {}])[1]
                        series_bars = series_map.get("sds_1", {}).get("s", [])
                        if series_bars:
                            for item in series_bars:
                                v = item.get("v", [])
                                if len(v) >= 5:
                                    ts = v[0]
                                    dt = datetime.datetime.fromtimestamp(
                                        ts, tz=datetime.timezone.utc
                                    )
                                    vol = float(v[5]) if len(v) > 5 and v[5] is not None else 0.0
                                    candles_out.append({
                                        "time": dt.strftime("%Y-%m-%d %H:%M"),
                                        "ts": ts,
                                        "open": float(v[1]),
                                        "high": float(v[2]),
                                        "low": float(v[3]),
                                        "close": float(v[4]),
                                        "volume": vol,
                                    })
                            return candles_out
                except Exception:
                    pass

    return candles_out


def analyze_cme_volume_footprint(
    candles: List[Dict[str, Any]], lookback: int = 14
) -> List[Dict[str, Any]]:
    """
    Analiza la huella de volumen en cada vela:
    - Compara volumen contra SMA(lookback) de contratos reales en CME.
    - Ratio de mecha vs cuerpo para detectar absorción institucional.
    """
    results = []
    for i in range(len(candles)):
        c = candles[i]
        # Calcular promedio de volumen móvil previo
        start_idx = max(0, i - lookback)
        prior_vols = [candles[j]["volume"] for j in range(start_idx, i) if candles[j]["volume"] > 0]
        avg_vol = (sum(prior_vols) / len(prior_vols)) if prior_vols else c["volume"]

        vol = c["volume"]
        vol_ratio = (vol / avg_vol) if avg_vol > 0 else 1.0

        # Rango y anatomía de la vela
        total_range = c["high"] - c["low"]
        body = abs(c["close"] - c["open"])
        upper_wick = c["high"] - max(c["open"], c["close"])
        lower_wick = min(c["open"], c["close"]) - c["low"]

        # Clasificación de volumen
        if vol_ratio >= 2.0:
            vol_tag = "CLÍMAX / PICO MASIVO (2x+)"
            whale_detected = True
        elif vol_ratio >= 1.5:
            vol_tag = "ALTO INSTITUCIONAL (1.5x+)"
            whale_detected = True
        elif vol_ratio <= 0.6:
            vol_tag = "BAJO / SIN INTERÉS (<0.6x)"
            whale_detected = False
        else:
            vol_tag = "NORMAL"
            whale_detected = False

        # Detección de Absorción (Wick Rejection con alto volumen)
        absorption = "NINGUNA"
        if whale_detected and total_range > 0:
            if lower_wick / total_range >= 0.45:
                absorption = "ABSORCIÓN COMPRADORA 🟢 (Rechazo de mínimos con acumulación)"
            elif upper_wick / total_range >= 0.45:
                absorption = "ABSORCIÓN VENDEDORA 🔴 (Rechazo de máximos con distribución)"

        results.append({
            "time": c["time"],
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "volume": vol,
            "avg_volume": avg_vol,
            "vol_ratio": vol_ratio,
            "vol_tag": vol_tag,
            "whale_detected": whale_detected,
            "absorption": absorption,
        })
    return results


def detect_smt_divergence(
    eur_candles: List[Dict[str, Any]], dxy_candles: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Detecta divergencia SMT (Smart Money Technique) entre EUR/USD y DXY.
    Normalmente son inversamente proporcionales (DXY sube -> EUR cae).
    Si uno rompe extremo y el otro falla, hay divergencia institucional.
    """
    if len(eur_candles) < 5 or len(dxy_candles) < 5:
        return {"divergence": "INSUFFICIENT_DATA"}

    # Últimas 3 velas
    eur_last = eur_candles[-1]
    eur_prev = eur_candles[-3]
    dxy_last = dxy_candles[-1]
    dxy_prev = dxy_candles[-3]

    eur_trend = "DOWN" if eur_last["close"] < eur_prev["close"] else "UP"
    dxy_trend = "UP" if dxy_last["close"] > dxy_prev["close"] else "DOWN"

    # Comparación de extremos (Highs y Lows)
    eur_made_lower_low = eur_last["low"] < eur_prev["low"]
    dxy_made_higher_high = dxy_last["high"] > dxy_prev["high"]

    eur_made_higher_high = eur_last["high"] > eur_prev["high"]
    dxy_made_lower_low = dxy_last["low"] < dxy_prev["low"]

    if dxy_made_higher_high and not eur_made_lower_low:
        return {
            "divergence": "BULLISH_SMT",
            "detail": "DXY marcó nuevo High pero EUR/USD resistió sin marcar nuevo Low (Acumulación de ballenas en EUR 🟢)",
        }
    elif not dxy_made_higher_high and eur_made_lower_low:
        return {
            "divergence": "BEARISH_SMT",
            "detail": "EUR/USD marcó nuevo Low pero DXY no confirmó el High (Venta institucional o debilidad en Dólar 🔴)",
        }
    elif dxy_made_lower_low and not eur_made_higher_high:
        return {
            "divergence": "BEARISH_SMT",
            "detail": "DXY marcó nuevo Low pero EUR/USD falló en romper su High (Falta de compradores en EUR 🔴)",
        }

    return {
        "divergence": "ALINEADOS",
        "detail": f"Correlación normal inversa respetada (EUR: {eur_trend}, DXY: {dxy_trend})",
    }


async def main():
    import websockets

    print("=" * 80)
    print("🐋 TEST: DETECCIÓN DE HUELLA INSTITUCIONAL (BALLENAS) CON TRADINGVIEW")
    print("=" * 80)

    # 1. Obtener Futuros CME del Euro (6E1!) y DXY
    print("📡 1. Descargando contratos reales de Futuros CME (6E1!) en M15...")
    cme_candles = await fetch_tv_candles("CME:6E1!", timeframe="15", n_bars=20)

    print("📡 2. Descargando velas de EUR/USD Spot (OANDA)...")
    eur_candles = await fetch_tv_candles("OANDA:EURUSD", timeframe="15", n_bars=20)

    print("📡 3. Descargando velas de Índice Dólar (TVC:DXY)...")
    dxy_candles = await fetch_tv_candles("TVC:DXY", timeframe="15", n_bars=20)

    if not cme_candles or not eur_candles or not dxy_candles:
        print("❌ Error: No se pudieron obtener todas las series de datos.")
        return

    print("\n✅ Datos sincronizados. Analizando volumen institucional de Futuros CME:")
    print("-" * 80)
    print(
        f"{'Fecha (UTC)':<16} | {'Cierre CME':<10} | {'Contratos':<10} | {'Promedio':<10} | {'Ratio':<6} | {'Diagnóstico':<20}"
    )
    print("-" * 80)

    footprints = analyze_cme_volume_footprint(cme_candles, lookback=10)

    whale_events = []
    # Mostramos las últimas 8 velas
    for f in footprints[-8:]:
        print(
            f"{f['time']:<16} | {f['close']:<10.4f} | {f['volume']:<10,.0f} | {f['avg_volume']:<10,.0f} | {f['vol_ratio']:<5.2f}x | {f['vol_tag']}"
        )
        if f["absorption"] != "NINGUNA":
            print(f"   ↳ 🎯 HUELLA DETECTADA: {f['absorption']}")
            whale_events.append(f)
        elif f["whale_detected"]:
            print(f"   ↳ ⚡ VOLUMEN INUSUAL: Entrada de volumen fuerte sin rechazo evidente.")
            whale_events.append(f)

    print("\n" + "=" * 80)
    print("🔍 2. ANÁLISIS DE DIVERGENCIA SMT (EUR/USD vs DXY)")
    print("=" * 80)
    smt = detect_smt_divergence(eur_candles, dxy_candles)
    print(f"Estado SMT: {smt['divergence']}")
    print(f"Detalle:    {smt['detail']}")

    print("\n" + "=" * 80)
    print("🧠 3. EVALUACIÓN DE CONFLUENCIA PARA ENTRADAS")
    print("=" * 80)

    last_f = footprints[-1]
    print(f"Última vela cerrada CME ({last_f['time']}):")
    print(f"  • Contratos negociados: {last_f['volume']:,.0f} (Promedio previo: {last_f['avg_volume']:,.0f})")
    print(f"  • Ratio de actividad:   {last_f['vol_ratio']:.2f}x")

    if last_f["vol_ratio"] >= 1.5:
        print("  • Veredicto Institucional: ✅ ALTA PARTICIPACIÓN (Ballenas activas en el mercado).")
        print("    -> Si tu setup SMC M15 tiene zona aquí, la entrada tiene ALTO respaldo institucional.")
    elif last_f["vol_ratio"] <= 0.65:
        print("  • Veredicto Institucional: ⚠️ MERCADO APÁTICO / BAJO VOLUMEN.")
        print("    -> Cuidado con rupturas (BOS): alta probabilidad de trampas o consolidación.")
    else:
        print("  • Veredicto Institucional: ⚪ PARTICIPACIÓN ESTÁNDAR.")


if __name__ == "__main__":
    asyncio.run(main())
