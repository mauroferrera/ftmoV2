"""Servicio de Plan de Trading 40 Minutos y Comité de Deliberación Multi-Moneda.

Arquitectura:
1. Al encender el proyecto (Startup):
   - Escaneo completo de mercado y noticias para todas las monedas (USD, EUR, GBP, CAD, CHF, JPY, AUD, NZD).
   - Agenda las horas exactas de noticias macroeconómicas previstas (Medium/High impact).
   - Si hay una noticia prevista dentro de los próximos 40 minutos, programa la re-deliberación
     exactamente en el momento del evento.
   - Si no hay noticias, delibera y emite el Plan de Trading para los próximos 40 minutos.
2. Cada 40 minutos (o al llegar una hora de noticia agendada):
   - El Comité delibera la estructura técnica (Discount/Premium, Fibonacci OTE 0.618-0.786, Order Blocks)
     y el sesgo macroeconómico de todas las monedas operables.
   - Establece el Mandato de 40 Minutos por par (COMPRA_EN_DISCOUNT, VENTA_EN_PREMIUM, CONTINUIDAD o PAUSA).
   - Guarda el plan en disco (database/trading_plan_40m.json).
3. Vigilante en Tiempo Real (AutoTrader cada 30 segundos):
   - El ejecutor vigila el precio y las posiciones abiertas respetando estrictamente el Plan de 40 Minutos.
   - Verifica el gatillo de Volumen VSA en TradingView antes de disparar órdenes en las zonas aprobadas.
   - Aplica protección inteligente a Break-Even ante agotamiento de volumen o llegada a 50% Equilibrium.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from api.services.eurusd_web_intel import (
    ALL_MAJOR_CURRENCIES,
    CALENDAR_JSON_URL,
    _HTTP_HEADERS,
    parse_calendar_json,
)
from core import clock, paths
from core.smc_engine import compute_ote_dealing_range
from settings import strategy_source

log = logging.getLogger("api.services.plan_40m")

DEFAULT_SYMBOLS: Tuple[str, ...] = (
    "EURUSD",
    "GBPUSD",
    "USDCAD",
    "USDCHF",
    "USDJPY",
    "EURJPY",
    "AUDUSD",
    "AUDCAD",
    "AUDNZD",
    "AUDCHF",
    "AUDJPY",
    "XAUUSD",
)


def get_configured_symbols() -> List[str]:
    """Obtiene la lista de símbolos configurados en strategy.yaml."""
    try:
        raw = strategy_source.get_config() or {}
        exec_cfg = raw.get("execution") or {}
        syms = exec_cfg.get("symbols_allow") or []
        if isinstance(syms, list) and syms:
            return [str(s).upper() for s in syms if s]
    except Exception:
        pass
    return list(DEFAULT_SYMBOLS)


def get_sl_distance(symbol: str) -> float:
    """Obtiene la distancia de Stop Loss para el símbolo desde strategy.yaml."""
    try:
        raw = strategy_source.get_config() or {}
        exec_cfg = raw.get("execution") or {}
        by_sym = exec_cfg.get("sl_distance_by_symbol") or {}
        if symbol in by_sym:
            return float(by_sym[symbol])
        if "sl_distance" in exec_cfg:
            return float(exec_cfg["sl_distance"])
    except Exception:
        pass
    return 0.0050


class Plan40mService:
    """Orquestador del Comité de Deliberación cada 40 minutos y escaneo de noticias."""

    def __init__(
        self,
        runtime: Any = None,
        state_file: Optional[str] = None,
    ) -> None:
        self._rt = runtime
        self._state_file = state_file or os.path.join(paths.DATABASE_DIR, "trading_plan_40m.json")
        self._enabled = False
        self._task: Optional[asyncio.Task] = None
        self._current_plan: Optional[Dict[str, Any]] = None
        self._scheduled_news: List[Dict[str, Any]] = []
        self._next_run_utc: Optional[datetime] = None
        self._next_trigger_reason: str = "ARRANQUE_INICIAL"
        self._last_deliberated_at: Optional[str] = None
        self._is_deliberating: bool = False

        # Cargar plan previo si existe en disco
        self._load_persisted_plan()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _load_persisted_plan(self) -> None:
        """Carga el último plan guardado si el fichero existe y es válido."""
        if os.path.exists(self._state_file):
            try:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and data.get("plan_id"):
                        self._current_plan = data
                        v_to = data.get("valid_to_utc")
                        if v_to:
                            try:
                                dt = datetime.fromisoformat(v_to.replace("Z", "+00:00"))
                                self._next_run_utc = dt
                            except Exception:
                                pass
            except Exception as exc:
                log.debug("No se pudo cargar plan previo desde disco: %s", exc)

    def _persist_plan(self, plan: Dict[str, Any]) -> None:
        """Persiste el plan en disco de manera atómica."""
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            tmp = self._state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._state_file)
        except Exception as exc:
            log.warning("No se pudo persistir el Plan 40m en %s: %s", self._state_file, exc)

    async def start(self) -> None:
        """Arranca el servicio y realiza el escaneo completo inicial de arranque."""
        if self._enabled:
            return
        self._enabled = True
        log.info("Iniciando Servicio de Plan de Trading 40 Minutos...")

        # Ejecutamos deliberación inicial completa en un hilo worker para no bloquear el bucle de eventos
        try:
            await asyncio.to_thread(self.deliberate_and_create_plan, force=True, reason="ARRANQUE_SISTEMA_SCAN_COMPLETO")
        except Exception as exc:
            log.error("Fallo en escaneo inicial de arranque de Plan 40m: %s", exc)

        # Iniciar bucle de vigilancia de tiempo y noticias
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Detiene el servicio en segundo plano."""
        self._enabled = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        log.info("Servicio de Plan de Trading 40 Minutos detenido.")

    async def _run_loop(self) -> None:
        """Bucle que vigila el reloj y dispara deliberaciones a los 40 min o al llegar noticias."""
        while self._enabled:
            try:
                await asyncio.sleep(5)
                if not self._enabled:
                    break

                now = datetime.now(timezone.utc)
                should_run = False
                trigger_reason = "CICLO_40_MIN"

                if self._next_run_utc is None:
                    should_run = True
                elif now >= self._next_run_utc:
                    should_run = True
                    trigger_reason = self._next_trigger_reason or "CICLO_40_MIN"

                if should_run and not self._is_deliberating:
                    log.info("Disparando deliberación de Plan 40 Min por: %s", trigger_reason)
                    await asyncio.to_thread(self.deliberate_and_create_plan, force=True, reason=trigger_reason)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("Error en bucle de Plan 40m: %s", exc)
                await asyncio.sleep(10)

    def fetch_all_macro_news(
        self,
        now_utc: Optional[datetime] = None,
        timeout_s: float = 4.0,
    ) -> List[Dict[str, Any]]:
        """Descarga y filtra eventos macroeconómicos para todas las divisas operables."""
        now = now_utc or datetime.now(timezone.utc)
        try:
            resp = requests.get(CALENDAR_JSON_URL, headers=_HTTP_HEADERS, timeout=timeout_s)
            if resp.status_code == 200:
                events = parse_calendar_json(
                    resp.json(),
                    now_utc=now,
                    allowed_currencies=ALL_MAJOR_CURRENCIES,
                )
                return events
        except Exception as exc:
            log.warning("No se pudo obtener calendario macroeconómico multi-divisa: %s", exc)
        return []

    def get_current_plan(self) -> Optional[Dict[str, Any]]:
        """Retorna el plan activo vigente."""
        return self._current_plan

    def deliberate_and_create_plan(
        self,
        force: bool = False,
        reason: str = "CICLO_40_MIN",
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Ejecuta la deliberación del Comité y construye el Plan para los próximos 40 minutos."""
        self._is_deliberating = True
        try:
            now = now_utc or datetime.now(timezone.utc)
            now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

            # 1. Escaneo completo de noticias para todas las monedas
            all_news = self.fetch_all_macro_news(now_utc=now)
            self._scheduled_news = all_news

            # Filtrar eventos futuros (dentro de las próximas 24 horas)
            upcoming = [
                ev for ev in all_news
                if ev.get("timestamp_utc") and ev["timestamp_utc"] >= now_iso
            ]
            upcoming.sort(key=lambda x: x.get("timestamp_utc", ""))

            # 2. Determinar la ventana de vigencia del Plan (40 min o hasta la próxima noticia)
            target_end = now + timedelta(minutes=40)
            next_event: Optional[Dict[str, Any]] = None
            next_trigger_label = "Ciclo regular (40 minutos)"

            for ev in upcoming:
                ts = ev.get("timestamp_utc")
                if not ts:
                    continue
                try:
                    ev_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    # Si hay una noticia en más de 2 minutos y antes de los 40 min
                    if now + timedelta(minutes=2) < ev_dt <= target_end:
                        target_end = ev_dt
                        next_event = ev
                        next_trigger_label = f"⚡ Noticia agendada: [{ev.get('currency')}] {ev.get('title')} ({ev.get('time_utc')})"
                        break
                    # Si la noticia es inminente (< 2 minutos)
                    elif ev_dt <= now + timedelta(minutes=2):
                        # La noticia está ocurriendo ahora: plan de transición de 15 minutos
                        target_end = now + timedelta(minutes=15)
                        next_event = ev
                        next_trigger_label = f"⚡ Noticia en curso: [{ev.get('currency')}] {ev.get('title')} (análisis post-noticia)"
                        break
                except Exception:
                    continue

            self._next_run_utc = target_end
            self._next_trigger_reason = next_trigger_label

            # 3. Integración viva con el Tribunal e Inteligencia (MT5 + SMC + Web + Consenso Bancario)
            tribunal_intel: Dict[str, Any] = {
                "has_tribunal": False,
                "bias": "neutral",
                "confidence": 50,
                "status": "APROBADO",
                "bank_sentiment": "neutral",
                "bank_bear_pct": 50.0,
                "bank_bull_pct": 30.0,
                "dollar_bias": "neutral",
                "ruling": "",
            }
            try:
                from api.services.eurusd_spot_analysis_service import EurUsdSpotAnalysisService
                from core.bank_consensus import load_bank_analyses_data

                spot_svc = EurUsdSpotAnalysisService()
                latest_spot = spot_svc.get_latest()
                bank_data = load_bank_analyses_data()

                # Consenso bancario (ING, Danske, Rabobank, etc.)
                b_meta = (bank_data.get("metadata") or {}).get("consensus") or {}
                b_dist = b_meta.get("bias_distribution") or {}
                b_bear = float(b_dist.get("bearish") or 50.0)
                b_bull = float(b_dist.get("bullish") or 30.0)

                if latest_spot:
                    payload = json.loads(latest_spot.get("payload_json") or "{}")
                    t_verdict = (payload.get("tribunal") or {}).get("verdict") or {}
                    t_bias = str(latest_spot.get("bias") or "neutral").lower()
                    t_conf = int(latest_spot.get("confidence") or 50)
                    t_status = str(t_verdict.get("status") or "APROBADO").upper()

                    # Brújula del Dólar: Si el Tribunal ve EUR/USD bajista, el Dólar (DXY) empuja al alza
                    d_bias = "alcista" if t_bias == "bajista" else ("bajista" if t_bias == "alcista" else "neutral")

                    tribunal_intel = {
                        "has_tribunal": True,
                        "bias": t_bias,
                        "confidence": t_conf,
                        "status": t_status,
                        "bank_bear_pct": b_bear,
                        "bank_bull_pct": b_bull,
                        "dollar_bias": d_bias,
                        "ruling": str(t_verdict.get("ruling") or ""),
                        "run_time": latest_spot.get("run_time"),
                        "generated_at_utc": latest_spot.get("generated_at_utc"),
                    }
            except Exception as exc:
                log.debug("No se pudo incorporar data del Tribunal en Plan 40m: %s", exc)

            # 4. Deliberación por Símbolo / Par para todas las monedas
            market_svc = getattr(self._rt, "market", None) if self._rt else None
            active_symbols = get_configured_symbols()
            if "XAUUSD" not in active_symbols:
                active_symbols.append("XAUUSD")

            pair_plans: Dict[str, Any] = {}
            for sym in active_symbols:
                plan_sym = self._deliberate_symbol_plan(
                    symbol=sym,
                    market_svc=market_svc,
                    upcoming_news=upcoming,
                    tribunal_intel=tribunal_intel,
                    now=now,
                )
                pair_plans[sym] = plan_sym

            # 5. Ensamblar documento del Plan 40 Minutos
            plan_doc: Dict[str, Any] = {
                "plan_id": f"PLAN-{now.strftime('%Y%m%d-%H%M')}",
                "generated_at_utc": clock.now_iso(),
                "valid_from_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "valid_to_utc": target_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration_minutes": max(5, int(round((target_end - now).total_seconds() / 60.0))),
                "reason": reason,
                "next_run_utc": target_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "next_trigger_label": next_trigger_label,
                "next_event_scheduled": next_event,
                "scheduled_news_count": len(upcoming),
                "scheduled_news": upcoming[:10],
                "tribunal_intel": tribunal_intel,
                "active_pairs_count": len(pair_plans),
                "pairs": pair_plans,
            }

            self._current_plan = plan_doc
            self._last_deliberated_at = clock.now_iso()
            self._persist_plan(plan_doc)

            log.info(
                "Plan 40 Min generado con éxito (%s). Vigente de %s a %s (%d min). Próximo: %s",
                plan_doc["plan_id"],
                plan_doc["valid_from_utc"],
                plan_doc["valid_to_utc"],
                plan_doc["duration_minutes"],
                next_trigger_label,
            )
            return plan_doc

        finally:
            self._is_deliberating = False

    def _deliberate_symbol_plan(
        self,
        symbol: str,
        market_svc: Optional[Any],
        upcoming_news: List[Dict[str, Any]],
        tribunal_intel: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Delibera la estrategia técnica, niveles y mandato específico para un par."""
        sym_clean = symbol.upper().replace("/", "")
        is_gold = sym_clean == "XAUUSD"

        # A) Revisar noticias que impacten a las monedas del par
        base_ccy = sym_clean[:3] if len(sym_clean) >= 6 else sym_clean
        quote_ccy = sym_clean[3:6] if len(sym_clean) >= 6 else ""

        imminent_news = []
        for ev in upcoming_news:
            ccy = str(ev.get("currency") or "").upper()
            if ccy in (base_ccy, quote_ccy):
                mins = ev.get("minutes_to_event")
                if mins is not None and 0 <= mins <= 25:
                    imminent_news.append(ev)

        # Si hay noticia de alto impacto en menos de 15 minutos: PAUSA PREVENTIVA
        if imminent_news and any(str(ev.get("impact", "")).lower() == "high" for ev in imminent_news):
            ev_title = imminent_news[0].get("title")
            ev_time = imminent_news[0].get("time_utc")
            return {
                "symbol": sym_clean,
                "mandate": "PAUSA_POR_NOTICIAS",
                "bias": "neutral",
                "status": "PAUSA",
                "location_pct": 50.0,
                "zone_type": "NOTICIAS_INMINENTES",
                "entry_zone": None,
                "sl_suggested": None,
                "tp_suggested": None,
                "invalidation": None,
                "trigger_condition": "Pausa activa: no operar hasta 15 min después de la publicación.",
                "rationale": f"Noticia de alto impacto inminente ({ev_title} a las {ev_time}). Riesgo de slippage y ensanchamiento de spread.",
                "is_manual_only": is_gold,
            }

        # B) Extraer datos de precio y rango diario (PDH/PDL) desde MarketService
        current_price = 1.0
        bid_p = 1.0
        ask_p = 1.0
        pdh: Optional[float] = None
        pdl: Optional[float] = None
        candles_m15: List[Dict[str, Any]] = []

        if market_svc is not None:
            try:
                p_info = market_svc.price(sym_clean)
                if p_info:
                    bid_p = float(p_info.get("bid") or 1.0)
                    ask_p = float(p_info.get("ask") or 1.0)
                    current_price = bid_p
            except Exception:
                pass

            try:
                p_data = market_svc.pattern_data(sym_clean, "M15", bars=100)
                if isinstance(p_data, dict):
                    candles_m15 = p_data.get("candles") or []
                    pdh = p_data.get("pdh")
                    pdl = p_data.get("pdl")
            except Exception:
                pass

        # Estimaciones si MT5 no tiene histórico
        if not pdh or not pdl or pdh <= pdl:
            if candles_m15 and len(candles_m15) >= 20:
                highs = [float(c["high"]) for c in candles_m15]
                lows = [float(c["low"]) for c in candles_m15]
                pdh = max(highs)
                pdl = min(lows)
            else:
                span = 0.0050 if "JPY" not in sym_clean and not is_gold else (0.50 if "JPY" in sym_clean else 15.0)
                pdh = round(current_price + span, 5)
                pdl = round(current_price - span, 5)

        rango = max(0.0001, pdh - pdl)
        loc_pct = round(((current_price - pdl) / rango) * 100.0, 1)
        loc_pct = max(0.0, min(100.0, loc_pct))
        eq_50 = round((pdh + pdl) / 2.0, 5)

        # SL default por símbolo
        sl_dist = get_sl_distance(sym_clean)

        # C) Cálculo de OTE (0.618 - 0.786)
        ote_info = compute_ote_dealing_range(candles_m15) if candles_m15 else None
        ote_dict = ote_info if isinstance(ote_info, dict) else {}
        ote_buy = ote_dict.get("ote_buy") or [round(pdl + rango * 0.214, 5), round(pdl + rango * 0.382, 5)]
        ote_sell = ote_dict.get("ote_sell") or [round(pdh - rango * 0.382, 5), round(pdh - rango * 0.214, 5)]

        # D) Veredicto del Comité para los próximos 40 minutos
        # Principio Maestro: Comprar barato en Discount (<= 35%) / Vender caro en Premium (>= 65%)
        if loc_pct <= 35.0:
            mandate = "COMPRA_EN_DISCOUNT"
            bias = "alcista"
            entry_min = round(pdl, 5)
            entry_max = round(pdl + rango * 0.25, 5)
            sl_sugg = round(pdl - sl_dist, 5)
            tp_sugg = eq_50
            inv_level = round(pdl - sl_dist * 0.5, 5)
            cond = f"Esperar precio en zona Discount [{entry_min:.5f} - {entry_max:.5f}] + Barra de Volumen VSA en TradingView"
            ratio_txt = f"Precio muy barato ({loc_pct}% del rango). Piso institucional PDL ({pdl:.5f}) cerca. Solo compras en absorción."

        elif loc_pct >= 65.0:
            mandate = "VENTA_EN_PREMIUM"
            bias = "bajista"
            entry_min = round(pdh - rango * 0.25, 5)
            entry_max = round(pdh, 5)
            sl_sugg = round(pdh + sl_dist, 5)
            tp_sugg = eq_50
            inv_level = round(pdh + sl_dist * 0.5, 5)
            cond = f"Esperar precio en zona Premium [{entry_min:.5f} - {entry_max:.5f}] + Barra de Volumen VSA en TradingView"
            ratio_txt = f"Precio muy caro ({loc_pct}% del rango). Techo institucional PDH ({pdh:.5f}) cerca. Solo ventas en absorción."

        else:
            mandate = "ESPERAR_ZONA"
            bias = "neutral"
            entry_min = None
            entry_max = None
            sl_sugg = None
            tp_sugg = None
            inv_level = None
            cond = "Precio en 50% Equilibrium. No perseguir en el centro; esperar retroceso a OTE o llegada a extremos."
            ratio_txt = f"Precio en zona neutral ({loc_pct}%). Prohibido entrar en el medio del rango sin confirmación clara."

        # E) Confluencia e Inteligencia del Tribunal y Consenso Bancario
        tribunal_alignment = ""
        status_plan = "ACTIVO"
        t_info = tribunal_intel or {}
        t_bias = str(t_info.get("bias") or "neutral").lower()
        t_status = str(t_info.get("status") or "APROBADO").upper()
        d_bias = str(t_info.get("dollar_bias") or "neutral").lower()

        if sym_clean == "EURUSD":
            if t_status == "VETO":
                mandate = "PAUSA_VETO_TRIBUNAL"
                status_plan = "VETO"
                tribunal_alignment = "⛔ VETO DEL TRIBUNAL: Evento de riesgo extremo o invalidación macro"
                ratio_txt = f"VETO activo del Tribunal Cuantitativo: {t_info.get('ruling', 'Operativa suspendida por riesgo')}"
            else:
                conf = t_info.get("confidence", 50)
                bear_pct = t_info.get("bank_bear_pct", 50.0)
                if mandate == "VENTA_EN_PREMIUM" and t_bias == "bajista":
                    tribunal_alignment = f"🌟 ALTA CONFLUENCIA: Techo Premium + Tribunal y Bancos Bajistas ({conf}% conf)"
                    ratio_txt += f" Respaldado por el Tribunal Macro y {bear_pct:.0f}% de consenso bancario bajista."
                elif mandate == "COMPRA_EN_DISCOUNT" and t_bias == "alcista":
                    tribunal_alignment = f"🌟 ALTA CONFLUENCIA: Piso Discount + Tribunal Alcista ({conf}% conf)"
                    ratio_txt += f" Respaldado por el Tribunal Macro ({conf}% conf)."
                elif mandate == "COMPRA_EN_DISCOUNT" and t_bias == "bajista":
                    tribunal_alignment = f"⚠️ CAUTELA: Piso Discount pero Tribunal Macro Bajista ({conf}% conf)"
                    ratio_txt += f" Cautela: El Tribunal y bancos proyectan presión bajista. Tomar TP en 50% Equilibrium."
                elif mandate == "VENTA_EN_PREMIUM" and t_bias == "alcista":
                    tribunal_alignment = f"⚠️ CAUTELA: Techo Premium pero Tribunal Macro Alcista ({conf}% conf)"
                    ratio_txt += f" Cautela: El Tribunal proyecta fuerza compradora. Salida estricta en 50% Equilibrium."
                else:
                    tribunal_alignment = f"⚖️ Tribunal EUR/USD: {t_bias.upper()} ({conf}% conf) | Bancos: {bear_pct:.0f}% Bear"
        elif "USD" in sym_clean:
            # Pares vinculados al Dólar (Brújula DXY del Tribunal)
            is_usd_base = sym_clean.startswith("USD")
            if is_usd_base:
                # USDCAD, USDCHF, USDJPY
                if d_bias == "alcista":
                    tribunal_alignment = "⚡ IMPULSO DXY ALCISTA (Dólar fuerte en Tribunal)"
                    if mandate == "COMPRA_EN_DISCOUNT":
                        tribunal_alignment = "🌟 ALTA CONFLUENCIA: Discount + Impulso Dólar Fuerte"
                elif d_bias == "bajista":
                    tribunal_alignment = "⚠️ PRESIÓN DXY BAJISTA (Dólar débil en Tribunal)"
            else:
                # GBPUSD, AUDUSD
                if d_bias == "alcista":
                    tribunal_alignment = "⚠️ PRESIÓN DXY FUERTE (Dólar fuerte en contra)"
                elif d_bias == "bajista":
                    tribunal_alignment = "⚡ IMPULSO DXY BAJISTA (Viento a favor de compras)"
                    if mandate == "COMPRA_EN_DISCOUNT":
                        tribunal_alignment = "🌟 ALTA CONFLUENCIA: Discount + Viento a favor (USD débil)"

        return {
            "symbol": sym_clean,
            "mandate": mandate,
            "bias": bias,
            "status": status_plan,
            "current_price": current_price,
            "pdh": pdh,
            "pdl": pdl,
            "eq_50": eq_50,
            "location_pct": loc_pct,
            "zone_type": "FIBO_OTE_EXTREMO" if entry_min is not None else "EQUILIBRIUM",
            "entry_zone": [entry_min, entry_max] if entry_min is not None else None,
            "sl_suggested": sl_sugg,
            "tp_suggested": tp_sugg,
            "invalidation": inv_level,
            "trigger_condition": cond,
            "rationale": ratio_txt,
            "tribunal_alignment": tribunal_alignment,
            "is_manual_only": is_gold,
        }

    def get_plan_with_vigilante_status(self) -> Dict[str, Any]:
        """Consolida el Plan 40m vigente con el estado en tiempo real del Vigilante."""
        plan = self._current_plan or {}
        now = datetime.now(timezone.utc)

        # Calcular minutos restantes de vigencia
        v_to = plan.get("valid_to_utc")
        mins_left = 0.0
        is_active = False
        if v_to:
            try:
                dt_to = datetime.fromisoformat(v_to.replace("Z", "+00:00"))
                diff_s = (dt_to - now).total_seconds()
                mins_left = max(0.0, round(diff_s / 60.0, 1))
                is_active = diff_s > 0
            except Exception:
                pass

        # Consultar estado del Vigilante (AutoTrader y ExecutionService)
        at_svc = getattr(self._rt, "autotrader", None) if self._rt else None
        exec_svc = getattr(self._rt, "execution", None) if self._rt else None

        at_enabled = bool(getattr(at_svc, "enabled", False))
        last_scan = getattr(at_svc, "_last_scan_at", None)
        last_rej = getattr(at_svc, "_last_rejection", None)

        open_positions: List[Dict[str, Any]] = []
        if exec_svc and hasattr(exec_svc, "positions"):
            try:
                pos_data, _ = exec_svc.positions()
                if pos_data:
                    for p in pos_data:
                        ticket = p.get("ticket")
                        view = {}
                        if hasattr(exec_svc, "management_view") and ticket:
                            try:
                                v_res = exec_svc.management_view(ticket)
                                view = v_res[0] if isinstance(v_res, tuple) else v_res
                            except Exception:
                                pass
                        open_positions.append({
                            "ticket": ticket,
                            "symbol": p.get("symbol"),
                            "direction": "BUY" if int(p.get("type", 0)) == 0 else "SELL",
                            "volume": p.get("volume"),
                            "price_open": p.get("price_open"),
                            "price_current": p.get("price_current"),
                            "profit": p.get("profit"),
                            "r_actual": view.get("r_actual") if isinstance(view, dict) else None,
                            "be_applied": view.get("be_applied") if isinstance(view, dict) else False,
                        })
            except Exception as exc:
                log.debug("No se pudieron leer posiciones para el vigilante: %s", exc)

        vigilante_status = {
            "status": "VIGILANDO_ACTIVO" if at_enabled else "PAUSADO_MANUAL",
            "autotrader_enabled": at_enabled,
            "scan_frequency_sec": 30,
            "last_scan_at": last_scan,
            "last_rejection": last_rej,
            "open_positions_count": len(open_positions),
            "open_positions": open_positions,
        }

        return {
            "is_active": is_active,
            "minutes_remaining": mins_left,
            "plan": plan,
            "vigilante": vigilante_status,
        }
