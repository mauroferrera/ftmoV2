"""Adaptador para el feed de datos institucional de TradingView (WebSocket).

Extrae:
- Velas y contratos de Futuros CME (6E1!, GC1!, 6B1!, 6A1!, etc.)
- Velas del Índice Dólar (TVC:DXY)
- Divergencias SMT y volumen de absorción institucional.

I/O aislado con caché en memoria y manejo de timeout/reintentos.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import random
import re
import string
import time
from typing import Any, Dict, List, Optional, Tuple

import websockets

from core import clock
from core.institutional_volume import (
    analyze_futures_volume,
    detect_smt_divergence,
    evaluate_institutional_confluence,
)

log = logging.getLogger(__name__)

#: Mapeo de pares forex/metales a sus contratos oficiales de futuros en CME/COMEX
CME_FUTURES_MAP: Dict[str, str] = {
    "EURUSD": "CME:6E1!",
    "GBPUSD": "CME:6B1!",
    "AUDUSD": "CME:6A1!",
    "USDCAD": "CME:6C1!",
    "USDCHF": "CME:6S1!",
    "USDJPY": "CME:6J1!",
    "XAUUSD": "COMEX:GC1!",
    # Cruces: fuerza de la divisa base
    "EURJPY": "CME:6E1!",
    "AUDCAD": "CME:6A1!",
    "AUDCHF": "CME:6A1!",
    "AUDJPY": "CME:6A1!",
    "AUDNZD": "CME:6A1!",
}

#: Pares donde USD es la divisa base (correlación directa con DXY y futuros CME en Divisa/USD invertidos)
USD_BASE_PAIRS = {"USDCAD", "USDJPY", "USDCHF"}

#: Pares donde USD es la divisa cotizada (correlación inversa con DXY)
USD_QUOTE_PAIRS = {"EURUSD", "GBPUSD", "AUDUSD", "XAUUSD"}

DXY_SYMBOL = "TVC:DXY"

#: Mapeo de timeframes estándar a string de TradingView
TV_TIMEFRAMES: Dict[str, str] = {
    "M1": "1",
    "M5": "5",
    "M15": "15",
    "M30": "30",
    "H1": "60",
    "H4": "240",
    "D1": "D",
}


#: Caché compartido a nivel de proceso para evitar reconexiones WebSocket redundantes
#: cuando se instancian múltiples TradingViewAdapter entre requests.
_SHARED_WS_CACHE: Dict[str, Tuple[float, Any]] = {}


def _format_ws_msg(func: str, param_list: list) -> str:
    payload = json.dumps({"m": func, "p": param_list}, separators=(",", ":"))
    return f"~m~{len(payload)}~m~{payload}"


class TradingViewAdapter:
    """Adaptador de datos de TradingView para volumen de futuros y macro."""

    def __init__(self, cache_ttl_seconds: float = 25.0) -> None:
        self.cache_ttl = cache_ttl_seconds
        # Caché de instancia: key -> (timestamp, data)
        self._cache: Dict[str, Tuple[float, Any]] = {}

    def _get_from_cache(self, key: str, shared: bool = True) -> Optional[Any]:
        # Revisar primero la caché de instancia
        if key in self._cache:
            ts, data = self._cache[key]
            if (time.time() - ts) < self.cache_ttl:
                return data
            del self._cache[key]
        # Si shared=True, revisar la caché compartida a nivel de proceso
        if shared and key in _SHARED_WS_CACHE:
            ts, data = _SHARED_WS_CACHE[key]
            if (time.time() - ts) < self.cache_ttl:
                return data
            del _SHARED_WS_CACHE[key]
        return None

    def _set_cache(self, key: str, data: Any, shared: bool = False) -> None:
        self._cache[key] = (time.time(), data)
        if shared:
            _SHARED_WS_CACHE[key] = (time.time(), data)

    def clear_cache(self) -> None:
        self._cache.clear()
        _SHARED_WS_CACHE.clear()

    async def fetch_candles_async(
        self,
        symbol: str,
        timeframe: str = "15",
        n_bars: int = 25,
        timeout_sec: float = 6.0,
    ) -> List[Dict[str, Any]]:
        """Descarga velas OHLCV desde el WebSocket oficial de TradingView."""
        cache_key = f"candles:{symbol}:{timeframe}:{n_bars}"
        cached = self._get_from_cache(cache_key, shared=True)
        if cached is not None:
            return cached

        tv_tf = TV_TIMEFRAMES.get(str(timeframe).upper(), str(timeframe))
        uri = "wss://data.tradingview.com/socket.io/websocket"
        session_id = f"cs_{''.join(random.choices(string.ascii_lowercase + string.digits, k=10))}"

        try:
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
                        "create_series", [session_id, "sds_1", "s1", "sds_sym", tv_tf, n_bars, ""]
                    )
                )

                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout_sec
                candles_out: List[Dict[str, Any]] = []

                while loop.time() < deadline:
                    try:
                        res = await asyncio.wait_for(ws.recv(), timeout=2.0)
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
                                            vol = (
                                                float(v[5])
                                                if len(v) > 5 and v[5] is not None
                                                else 0.0
                                            )
                                            candles_out.append({
                                                "time": dt.strftime("%Y-%m-%d %H:%M"),
                                                "ts": ts,
                                                "open": float(v[1]),
                                                "high": float(v[2]),
                                                "low": float(v[3]),
                                                "close": float(v[4]),
                                                "volume": vol,
                                            })
                                    if candles_out:
                                        self._set_cache(cache_key, candles_out, shared=True)
                                        return candles_out
                        except Exception:
                            pass
        except Exception as exc:
            log.warning("error al consultar WebSocket TradingView para %s: %s", symbol, exc)

        return []

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str = "15",
        n_bars: int = 25,
        timeout_sec: float = 6.0,
    ) -> List[Dict[str, Any]]:
        """Versión síncrona segura para llamar desde endpoints o servicios síncronos."""
        cache_key = f"candles:{symbol}:{timeframe}:{n_bars}"
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            return cached

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    self.fetch_candles_async(symbol, timeframe, n_bars, timeout_sec),
                )
                return future.result(timeout=timeout_sec + 2.0)
        else:
            return asyncio.run(
                self.fetch_candles_async(symbol, timeframe, n_bars, timeout_sec)
            )

    def get_cme_symbol_for(self, symbol: str) -> Optional[str]:
        clean = (symbol or "").upper().replace("/", "")
        return CME_FUTURES_MAP.get(clean)

    def get_institutional_analysis(
        self, symbol: str, timeframe: str = "M15", lookback: int = 12
    ) -> Dict[str, Any]:
        """
        Calcula el análisis institucional completo para un símbolo:
        - Volumen real de contratos de futuros CME (evaluando vela cerrada + viva con prefer_closed=True)
        - Liquidez interbancaria en tiempo real (0s delay via OANDA:<SYM>) para cubrir la ventana diferida
        - Detección de divergencia SMT con DXY (si aplica)
        """
        clean_sym = (symbol or "").upper().replace("/", "")
        cme_sym = self.get_cme_symbol_for(clean_sym)

        cache_key = f"analysis:{clean_sym}:{timeframe}"
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            return cached

        futures_analysis: Dict[str, Any] = {"available": False}
        if cme_sym:
            cme_candles = self.fetch_candles(cme_sym, timeframe=timeframe, n_bars=lookback + 5)
            if cme_candles:
                futures_analysis = analyze_futures_volume(
                    cme_candles, lookback=lookback, prefer_closed=True
                )
                futures_analysis["cme_symbol"] = cme_sym

        # Liquidez institucional en tiempo real (0s delay) via OANDA:<SYM>
        rt_analysis: Dict[str, Any] = {"available": False}
        rt_sym = f"OANDA:{clean_sym}"
        spot_candles: List[Dict[str, Any]] = []
        try:
            spot_candles = self.fetch_candles(rt_sym, timeframe=timeframe, n_bars=lookback + 5)
            if spot_candles:
                rt_analysis = analyze_futures_volume(
                    spot_candles, lookback=lookback, prefer_closed=True
                )
                rt_analysis["rt_symbol"] = rt_sym
        except Exception as exc:
            log.debug("no se pudo consultar volumen realtime %s: %s", rt_sym, exc)

        # SMT Divergence con DXY solo para pares con pata USD (0s delay en ambos feeds)
        smt_res: Optional[Dict[str, Any]] = None
        if clean_sym in USD_QUOTE_PAIRS or clean_sym in USD_BASE_PAIRS:
            try:
                if not spot_candles:
                    spot_candles = self.fetch_candles(
                        f"FX_IDC:{clean_sym}", timeframe=timeframe, n_bars=10
                    )
                dxy_candles = self.fetch_candles(DXY_SYMBOL, timeframe=timeframe, n_bars=10)
                if spot_candles and dxy_candles:
                    smt_res = detect_smt_divergence(
                        spot_candles[-10:],
                        dxy_candles[-10:],
                        inverse=(clean_sym in USD_QUOTE_PAIRS),
                    )
            except Exception as exc:
                log.debug("no se pudo calcular SMT para %s: %s", clean_sym, exc)

        result = {
            "symbol": clean_sym,
            "cme_symbol": cme_sym,
            "timeframe": timeframe,
            "futures": futures_analysis,
            "realtime": rt_analysis,
            "smt": smt_res,
            "updated_at": clock.now_iso(),
        }
        self._set_cache(cache_key, result)
        return result

    def evaluate_confluence(
        self, symbol: str, direction: str, timeframe: str = "M15"
    ) -> Dict[str, Any]:
        """Evalúa si un setup técnico tiene confluencia con el volumen institucional."""
        clean_sym = (symbol or "").upper().replace("/", "")
        analysis = self.get_institutional_analysis(clean_sym, timeframe)
        futures = analysis.get("futures") or {}
        realtime = analysis.get("realtime") or {}
        smt = analysis.get("smt")
        inverted = clean_sym in USD_BASE_PAIRS
        confluence = evaluate_institutional_confluence(
            direction,
            futures,
            smt,
            inverted_future=inverted,
            realtime_analysis=realtime,
        )
        confluence["cme_symbol"] = analysis.get("cme_symbol")
        confluence["symbol"] = clean_sym
        return confluence

