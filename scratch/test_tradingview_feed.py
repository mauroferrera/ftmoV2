"""
Script de prueba: Extracción directa de datos de TradingView (WebSocket + Scanner)
Sin navegador, sin Selenium/Playwright, sin credenciales de pago.

Obtiene:
1. Velas OHLCV (Open, High, Low, Close, Volume) vía WebSocket oficial en tiempo real.
2. Lecturas técnicas (RSI, MA, recomendación) vía Scanner API.
"""

import asyncio
import datetime
import io
import json
import random
import re
import string
import sys
from typing import Any, Dict, List, Optional

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests
import websockets


def _generate_session_id(prefix: str = "cs_") -> str:
    random_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return f"{prefix}{random_str}"


def _format_ws_msg(func: str, param_list: list) -> str:
    payload = json.dumps({"m": func, "p": param_list}, separators=(",", ":"))
    return f"~m~{len(payload)}~m~{payload}"


async def get_tradingview_candles(
    symbol: str = "OANDA:EURUSD",
    timeframe: str = "15",
    n_bars: int = 15,
    timeout_sec: float = 8.0,
) -> List[Dict[str, Any]]:
    """
    Descarga las últimas n_bars velas OHLCV desde el WebSocket de TradingView.

    :param symbol: Ticker completo (ej: 'OANDA:EURUSD', 'CME:6E1!', 'OANDA:XAUUSD', 'TVC:DXY')
    :param timeframe: '1', '5', '15', '60', '240', 'D'
    :param n_bars: Cantidad de velas a recuperar
    :param timeout_sec: Timeout máximo
    """
    uri = "wss://data.tradingview.com/socket.io/websocket"
    session_id = _generate_session_id("cs_")

    async with websockets.connect(
        uri,
        origin="https://www.tradingview.com",
        user_agent_header="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        open_timeout=timeout_sec,
    ) as ws:
        # 1. Autenticación como usuario anónimo público
        await ws.send(_format_ws_msg("set_auth_token", ["unauthorized_user_token"]))

        # 2. Inicializar sesión de gráfico
        await ws.send(_format_ws_msg("chart_create_session", [session_id, ""]))

        # 3. Resolver símbolo
        sym_spec = json.dumps({"adjustment": "splits", "symbol": symbol})
        await ws.send(_format_ws_msg("resolve_symbol", [session_id, "sds_sym_1", "=" + sym_spec]))

        # 4. Pedir serie de velas
        await ws.send(
            _format_ws_msg(
                "create_series",
                [session_id, "sds_1", "s1", "sds_sym_1", str(timeframe), n_bars, ""],
            )
        )

        # 5. Escuchar respuestas del socket
        deadline = asyncio.get_event_loop().time() + timeout_sec
        candles_out: List[Dict[str, Any]] = []

        while asyncio.get_event_loop().time() < deadline:
            try:
                res = await asyncio.wait_for(ws.recv(), timeout=2.5)
            except asyncio.TimeoutError:
                break

            # Desempaquetar tramas de socket.io (~m~len~m~json)
            parts = re.split(r"~m~\d+~m~", res)
            for part in parts:
                if not part:
                    continue
                # Mantener conexión viva con heartbeat
                if part.startswith("~h~"):
                    await ws.send(f"~m~{len(part)}~m~{part}")
                    continue

                try:
                    payload = json.loads(part)
                    msg_type = payload.get("m")

                    if msg_type == "timescale_update":
                        series_map = payload.get("p", [None, {}])[1]
                        series_bars = series_map.get("sds_1", {}).get("s", [])
                        if series_bars:
                            for item in series_bars:
                                # bar: [timestamp, open, high, low, close, volume]
                                v = item.get("v", [])
                                if len(v) >= 5:
                                    ts = v[0]
                                    dt = datetime.datetime.fromtimestamp(
                                        ts, tz=datetime.timezone.utc
                                    )
                                    vol = float(v[5]) if len(v) > 5 and v[5] is not None else 0.0
                                    candles_out.append({
                                        "time": dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                                        "timestamp": ts,
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


def get_tradingview_screener(
    symbol: str = "OANDA:EURUSD", market: str = "forex"
) -> Optional[Dict[str, Any]]:
    """
    Obtiene métricas técnicas instantáneas de TradingView Screener (RSI, MAs, recomendación).
    """
    url = f"https://scanner.tradingview.com/{market}/scan"
    payload = {
        "symbols": {"tickers": [symbol]},
        "columns": [
            "close",
            "open",
            "high",
            "low",
            "volume",
            "change",
            "RSI",
            "SMA20",
            "SMA50",
            "Recommend.All",
            "Recommend.MA",
            "Recommend.Other",
        ],
    }
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            items = data.get("data", [])
            if items:
                d = items[0].get("d", [])
                return {
                    "symbol": symbol,
                    "close": d[0],
                    "open": d[1],
                    "high": d[2],
                    "low": d[3],
                    "volume": d[4],
                    "change_pct": d[5],
                    "rsi_14": d[6],
                    "sma_20": d[7],
                    "sma_50": d[8],
                    "recommendation": d[9],  # Rango de -1 (Strong Sell) a +1 (Strong Buy)
                }
    except Exception as exc:
        print(f"[Aviso] No se pudo consultar el screener: {exc}", file=sys.stderr)
    return None


def _rating_label(score: Optional[float]) -> str:
    if score is None:
        return "N/A"
    if score >= 0.5:
        return "FUERTE COMPRA (Strong Buy) 🟢🟢"
    if score > 0.1:
        return "COMPRA (Buy) 🟢"
    if score <= -0.5:
        return "FUERTE VENTA (Strong Sell) 🔴🔴"
    if score < -0.1:
        return "VENTA (Sell) 🔴"
    return "NEUTRAL ⚪"


async def main():
    print("=" * 75)
    print("🚀 TEST DE CONEXIÓN A TRADINGVIEW (WEBSOCKET + SCANNER)")
    print("=" * 75)

    test_symbols = [
        ("OANDA:EURUSD", "forex", "EUR/USD Spot (Forex)"),
        ("CME:6E1!", "america", "EUR Futuros CME (Volumen Real de Exchange)"),
        ("OANDA:XAUUSD", "forex", "XAU/USD Oro Spot"),
        ("TVC:DXY", "cfd", "Índice Dólar DXY (Macro)"),
    ]

    for sym, market, desc in test_symbols:
        print(f"\n📡 Conectando a TradingView para: {desc} [{sym}]...")

        # 1. Velas M15 vía WebSocket
        candles = await get_tradingview_candles(sym, timeframe="15", n_bars=8)

        if not candles:
            print(f"❌ No se recibieron velas para {sym}")
            continue

        print(f"✅ ¡Conexión WebSocket exitosa! Recibidas {len(candles)} velas (M15):")
        print(
            f"{'Fecha y Hora':<22} | {'Open':<8} | {'High':<8} | {'Low':<8} | {'Close':<8} | {'Volumen':<10}"
        )
        print("-" * 75)
        for c in candles:
            vol_str = f"{c['volume']:,.0f}" if c["volume"] > 0 else "-"
            print(
                f"{c['time']:<22} | {c['open']:<8.4f} | {c['high']:<8.4f} | {c['low']:<8.4f} | {c['close']:<8.4f} | {vol_str:<10}"
            )

        # Estadísticas rápidas de volumen
        vols = [c["volume"] for c in candles if c["volume"] > 0]
        if vols:
            avg_vol = sum(vols) / len(vols)
            last_vol = vols[-1]
            spike_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0
            print(
                f"   📊 Volumen última vela: {last_vol:,.0f} | Promedio ({len(vols)} velas): {avg_vol:,.0f} | Ratio: {spike_ratio:.2f}x"
            )

        # 2. Resumen técnico vía Scanner
        screener = get_tradingview_screener(sym, market=market)
        if screener:
            rating = _rating_label(screener.get("recommendation"))
            rsi = screener.get("rsi_14")
            rsi_str = f"{rsi:.2f}" if rsi is not None else "N/A"
            print(f"   💡 Resumen Técnico TradingView:")
            print(f"      - Veredicto: {rating}")
            print(f"      - RSI(14): {rsi_str} | Cambio: {screener.get('change_pct', 0.0):+.2f}%")


if __name__ == "__main__":
    asyncio.run(main())
