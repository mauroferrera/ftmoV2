"""Servicio de reconciliación de resultados y auditoría post-mortem.

M20 (P0): Conecta las operaciones cerradas en el broker (history_deals_get)
con `setup_log`, `position_state` y `trade_marks`.

Sin este servicio, el sistema opera a ciegas: no hay R realizado, no hay PnL
real auditado, no se conocen las desviaciones MAE/MFE ni los motivos reales
de salida (SL, TP, BE, parcial, cierre manual).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from core import clock

log = logging.getLogger("api.services.reconciliation")

# Razones de deal de MT5
DEAL_REASON_CLIENT = 0
DEAL_REASON_MOBILE = 1
DEAL_REASON_WEB = 2
DEAL_REASON_EXPERT = 3
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
DEAL_REASON_SO = 6

# Entry types de deal MT5
DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_ENTRY_OUT_BY = 3


class ReconciliationService:
    """Reconcilia los deals del broker con setup_log y trade_marks."""

    def __init__(self, market: Any, store: Any, execution: Optional[Any] = None) -> None:
        self._market = market
        self._store = store
        self._execution = execution

    def reconcile_closed_trades(self, days: int = 30) -> Dict[str, Any]:
        """Lee deals de MT5, empareja entradas y salidas, y escribe en `trade_marks`.
        
        Es completamente idempotente (ON CONFLICT DO UPDATE por ticket de posición).
        Retorna un resumen de la sincronización.
        """
        if self._market is None or self._store is None:
            return {"status": "error", "message": "Market o Store no disponible", "reconciled": 0}

        try:
            deals = self._market.history_deals(days=days)
        except Exception as exc:
            log.warning("No se pudieron leer history_deals: %s", exc)
            return {"status": "error", "message": str(exc), "reconciled": 0}

        if not deals:
            return {"status": "ok", "message": "No hay deals en el historial", "reconciled_positions": 0, "marks_saved": 0}

        # 1. Agrupar deals por position_id (el ticket de la posición en MT5)
        deals_by_pos: Dict[int, List[Dict[str, Any]]] = {}
        for d in deals:
            pos_id = int(d.get("position_id") or 0)
            if pos_id > 0:
                deals_by_pos.setdefault(pos_id, []).append(d)

        # 2. Cargar setups recientes para emparejar
        recent_setups = []
        try:
            recent_setups = self._store.list_setup_log(limit=2000)
        except Exception as exc:
            log.warning("No se pudo listar setup_log: %s", exc)

        # Indexar setups por TICKET DE POSICIÓN. `trade_result_json["order"]` es
        # el ticket de la ORDER de MT5, que NO es el `position_id` del deal: en
        # una cuenta hedging son números distintos, y emparejar por `order`
        # dejaba posiciones sin setup (M20). `order` queda como reserva para las
        # filas escritas antes de que existiera `position_ticket`.
        setup_by_order: Dict[int, Dict[str, Any]] = {}
        for s in recent_setups:
            tr = s.get("trade_result_json") or {}
            for clave in ("position_ticket", "order"):
                ident = tr.get(clave)
                if not ident:
                    continue
                try:
                    setup_by_order.setdefault(int(ident), s)
                except (ValueError, TypeError):
                    continue

        marks_saved = 0
        reconciled_positions = 0

        # 3. Procesar cada posición
        for pos_id, p_deals in deals_by_pos.items():
            # Separar deals de entrada y salida
            in_deals = [d for d in p_deals if d.get("entry") == DEAL_ENTRY_IN]
            out_deals = [d for d in p_deals if d.get("entry") in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY)]

            # Solo nos interesan posiciones que ya tienen al menos una salida registrada
            if not out_deals:
                continue

            # Deal de apertura
            in_deal = in_deals[0] if in_deals else None
            symbol = str((in_deal or out_deals[0]).get("symbol") or "")
            direction = "BUY" if (in_deal or out_deals[0]).get("type") == 0 else "SELL"

            time_open = in_deal.get("time") if in_deal else out_deals[0].get("time")
            price_open = float(in_deal.get("price") or 0.0) if in_deal else float(out_deals[0].get("price") or 0.0)
            in_volume = float(in_deal.get("volume") or 0.0) if in_deal else 0.0

            # Métricas agregadas de las salidas y costos totales de la posición
            total_out_vol = sum(float(d.get("volume") or 0.0) for d in out_deals)
            total_profit_raw = sum(float(d.get("profit") or 0.0) for d in out_deals)
            total_comm = sum(float(d.get("commission") or 0.0) for d in p_deals)
            total_swap = sum(float(d.get("swap") or 0.0) for d in p_deals)
            total_fee = sum(float(d.get("fee") or 0.0) for d in p_deals)

            # PnL neto real
            net_profit = round(total_profit_raw + total_comm + total_swap + total_fee, 2)

            # Precio promedio ponderado de salida
            weighted_close_sum = sum(float(d.get("price") or 0.0) * float(d.get("volume") or 0.0) for d in out_deals)
            price_close = round(weighted_close_sum / total_out_vol, 5) if total_out_vol > 0 else float(out_deals[-1].get("price") or 0.0)

            time_close = max(d.get("time") or 0 for d in out_deals)
            duration_s = max(0.0, float(time_close - (time_open or time_close)))

            # Último deal determina el motivo de salida principal
            last_out_deal = sorted(out_deals, key=lambda x: x.get("time") or 0)[-1]
            exit_reason = int(last_out_deal.get("reason") or 0)

            # 4. Vincular con setup_log
            setup = setup_by_order.get(pos_id)
            if not setup and in_deal:
                # Fallback: buscar por símbolo, dirección y cercanía temporal (± 180s)
                setup = self._match_setup_fallback(recent_setups, symbol, direction, in_deal.get("time") or 0)

            setup_id = setup.get("id") if setup else None
            initial_sl = float(setup.get("sl") or 0.0) if setup else 0.0
            initial_tp = float(setup.get("target") or 0.0) if setup else 0.0
            score = float(setup.get("score") or 0.0) if setup else None
            source = str(setup.get("source") or "reconciled") if setup else "broker_deal"

            # 5. Estado de la posición: de ahí sale la DISTANCIA DE RIESGO original
            pos_state = None
            try:
                fn_pos_state = getattr(self._store, "get_position_state", None)
                if fn_pos_state:
                    pos_state = fn_pos_state(pos_id)
            except Exception:
                pass

            # El R se mide contra la distancia de riesgo de la APERTURA, no
            # contra el stop actual: en cuanto el SL se mueve a break-even,
            # `|entrada - SL actual|` es ~0 y el R se dispara. `r_original` de
            # `position_state` es la fuente; el `sl` del setup, la reserva.
            # `be_level` NO sirve para esto: es el stop de HOY, no el riesgo.
            risk_dist: Optional[float] = None
            try:
                r_original = (pos_state or {}).get("r_original")
                if r_original is not None and abs(float(r_original)) > 1e-9:
                    risk_dist = abs(float(r_original))
            except (TypeError, ValueError):
                risk_dist = None
            if risk_dist is None and initial_sl > 0 and price_open > 0:
                medida = abs(price_open - initial_sl)
                risk_dist = medida if medida > 1e-6 else None

            # 6. Cálculo de R-múltiplo realizado (`None` si no se puede medir)
            r_multiple = self._calc_r_multiple(
                direction=direction,
                price_open=price_open,
                price_close=price_close,
                risk_dist=risk_dist,
            )

            # 7. Clasificación de Calidad de Salida (exit_quality)
            exit_quality = self._classify_exit_quality(
                exit_reason=exit_reason,
                net_profit=net_profit,
                r_multiple=r_multiple,
                price_open=price_open,
                price_close=price_close,
                initial_sl=initial_sl,
                initial_tp=initial_tp,
                direction=direction,
            )

            # 8. MAE / MFE (si hay velas disponibles en market)
            mae_r, mfe_r = self._calc_mae_mfe(
                symbol=symbol,
                direction=direction,
                price_open=price_open,
                risk_dist=risk_dist,
                time_open=time_open,
                time_close=time_close,
            )

            # 9. Construir outcome_json y context_json
            # Una posición con salida PARCIAL sigue viva en el bróker: la marca
            # se guarda igual (es una decisión ya ejecutada), pero se publica
            # `is_open` para que el análisis de rendimiento no cuente como
            # cerrada una operación que aún puede girar.
            is_open = in_volume > 0 and total_out_vol < in_volume * 0.99
            outcome_json = {
                "net_profit": net_profit,
                "profit_raw": round(total_profit_raw, 2),
                "commission": round(total_comm, 2),
                "swap": round(total_swap, 2),
                "fee": round(total_fee, 2),
                "exit_deals_count": len(out_deals),
                "deal_tickets": [d.get("ticket") for d in out_deals],
                "exit_type": exit_quality,
                "mae_r": mae_r,
                "mfe_r": mfe_r,
                "is_open": is_open,
                "is_full_close": not is_open,
                "risk_dist": round(risk_dist, 5) if risk_dist else None,
            }

            score_bucket = self._bucket_score(score) if score is not None else "no_score"
            context_json = {
                "score": score,
                "score_bucket": score_bucket,
                "verdict": setup.get("verdict") if setup else "UNKNOWN",
                "setup_id": setup_id,
                "source": source,
                "context_snapshot": setup.get("context_json") if setup else {},
            }

            # 10. Persistir marca en `trade_marks`
            mark_data = {
                "ticket": pos_id,
                "symbol": symbol,
                "direction": direction,
                "setup_id": setup_id,
                "volume": total_out_vol,
                "price_open": price_open,
                "price_close": price_close,
                "sl": initial_sl if initial_sl > 0 else None,
                "tp": initial_tp if initial_tp > 0 else None,
                "profit": net_profit,
                "r_multiple": r_multiple,
                "exit_reason": exit_reason,
                "exit_quality": exit_quality,
                "duration_s": duration_s,
                "capture_path": None,
                "outcome_json": outcome_json,
                "context_json": context_json,
                "source": source,
                "created_at": clock.now_iso(),
            }

            try:
                self._store.save_trade_mark(mark_data)
                marks_saved += 1
            except Exception as exc:
                log.error("Error al guardar trade_mark para pos_id %s: %s", pos_id, exc)

            # 11. Si tenemos setup_id, actualizar setup_log con el resultado final
            if setup_id:
                try:
                    self._store.update_trade_result(setup_id, {
                        "ticket": pos_id,
                        "status": "CLOSED",
                        "net_profit": net_profit,
                        "r_multiple": r_multiple,
                        "exit_quality": exit_quality,
                        "price_close": price_close,
                        "closed_at": clock.now_iso(),
                    })
                except Exception as exc:
                    log.warning("No se pudo actualizar trade_result en setup_id %s: %s", setup_id, exc)

            reconciled_positions += 1

        return {
            "status": "ok",
            "reconciled_positions": reconciled_positions,
            "marks_saved": marks_saved,
            "total_deals_analyzed": len(deals),
        }

    def _match_setup_fallback(
        self,
        setups: List[Dict[str, Any]],
        symbol: str,
        direction: str,
        time_open: int,
    ) -> Optional[Dict[str, Any]]:
        """Busca setup por proximidad de tiempo si no había order ID explícito."""
        for s in setups:
            if str(s.get("symbol")).upper() != symbol.upper():
                continue
            if str(s.get("direction")).upper() != direction.upper():
                continue
            # Comparar timestamp si es convertible a epoch
            ts = s.get("timestamp") or s.get("created_at")
            if ts:
                try:
                    import datetime
                    dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    epoch = dt.timestamp()
                    if abs(epoch - time_open) <= 300:  # 5 minutos
                        return s
                except Exception:
                    pass
        return None

    def _calc_r_multiple(
        self,
        direction: str,
        price_open: float,
        price_close: float,
        risk_dist: Optional[float],
    ) -> Optional[float]:
        """El R realizado, o `None` si no se puede medir.

        Sin la distancia de riesgo de la apertura no hay R que calcular. Antes
        se devolvía `net_profit / 125.0`, que es el riesgo nominal de una cuenta
        de 25k al 0.5% —el de OTRA operación— y convertía cualquier PnL en un R
        inventado. Un `None` en la auditoría es un dato que falta; un número
        inventado es un dato falso.
        """
        if not risk_dist or float(risk_dist) <= 1e-9 or price_open <= 0:
            return None
        rd = float(risk_dist)
        if direction.upper() == "BUY":
            return round((price_close - price_open) / rd, 2)
        return round((price_open - price_close) / rd, 2)

    def _classify_exit_quality(
        self,
        exit_reason: int,
        net_profit: float,
        r_multiple: Optional[float],
        price_open: float,
        price_close: float,
        initial_sl: float,
        initial_tp: float,
        direction: str,
    ) -> str:
        """Clasifica la calidad/tipo de salida para auditoría.

        El break-even se decide por R (`|R| <= 0.15`), no por un umbral en
        dólares: `|PnL| <= $5` no significa lo mismo en EURUSD que en XAUUSD
        con 0.5 lotes. Sin R medible se clasifica por el motivo del deal, que
        es el dato que el bróker sí da.
        """
        be = r_multiple is not None and abs(float(r_multiple)) <= 0.15
        if exit_reason == DEAL_REASON_SL:
            return "BE" if be else "SL"
        elif exit_reason == DEAL_REASON_TP:
            return "TP"
        elif exit_reason == DEAL_REASON_SO:
            return "STOP_OUT"
        elif exit_reason in (DEAL_REASON_CLIENT, DEAL_REASON_MOBILE, DEAL_REASON_WEB):
            if be:
                return "MANUAL_BE"
            return "MANUAL_WIN" if net_profit > 0 else "MANUAL_LOSS"
        elif exit_reason == DEAL_REASON_EXPERT:
            if be:
                return "BE"
            if r_multiple is not None and float(r_multiple) >= 0.8:
                return "TP_PARTIAL" if float(r_multiple) < 1.8 else "TP"
            return "MANAGEMENT"

        # Fallback genérico
        if net_profit > 0:
            return "WIN"
        elif net_profit < 0:
            return "LOSS"
        return "BE"

    def _calc_mae_mfe(
        self,
        symbol: str,
        direction: str,
        price_open: float,
        risk_dist: Optional[float],
        time_open: Optional[int],
        time_close: Optional[int],
    ) -> Tuple[Optional[float], Optional[float]]:
        """Calcula MAE y MFE en unidades de R a partir de las velas del trade."""
        if not time_open or not time_close or not risk_dist or price_open <= 0:
            return None, None

        rd = abs(float(risk_dist))
        if rd <= 1e-9:
            return None, None

        # Intentar obtener velas de mercado para el trade
        try:
            # Leemos velas M1 o M15 si market dispone del método
            candles = getattr(self._market, "candles", None)
            if not candles:
                return None, None
            # Si market.candles devuelve dict con 'candles'
            bars_resp = candles(symbol, "M15")
            bars = bars_resp.get("candles") if isinstance(bars_resp, dict) else bars_resp
            if not bars:
                return None, None

            # Filtrar barras en el rango [time_open, time_close]
            trade_bars = [b for b in bars if time_open <= int(b.get("time") or 0) <= time_close + 900]
            if not trade_bars:
                return None, None

            highs = [float(b.get("high") or 0.0) for b in trade_bars]
            lows = [float(b.get("low") or 0.0) for b in trade_bars]
            max_high = max(highs) if highs else price_open
            min_low = min(lows) if lows else price_open

            if direction.upper() == "BUY":
                mae_dist = max(0.0, price_open - min_low)
                mfe_dist = max(0.0, max_high - price_open)
            else:
                mae_dist = max(0.0, max_high - price_open)
                mfe_dist = max(0.0, price_open - min_low)

            return round(mae_dist / rd, 2), round(mfe_dist / rd, 2)
        except Exception:
            return None, None

    def _bucket_score(self, score: Optional[float]) -> str:
        """Agrupa el score en buckets de análisis."""
        if score is None:
            return "no_score"
        if score < 60.0:
            return "<60"
        elif score < 70.0:
            return "60-70"
        elif score < 80.0:
            return "70-80"
        return "80+"

    def get_performance_analysis(self, limit: int = 50) -> Dict[str, Any]:
        """Calcula el análisis de rendimiento enriquecido para `/api/audit/performance`."""
        if self._store is None:
            return {"error": "Store no disponible"}

        # Cargar trade marks
        try:
            marks = self._store.list_trade_marks(limit=1000)
        except Exception as exc:
            log.warning("Error al listar trade_marks: %s", exc)
            marks = []

        # Una marca con `is_open` es una salida PARCIAL de una posición que
        # sigue viva: contarla como trade cerrado inflaba el winrate con
        # operaciones que aún pueden girar.
        open_marks = [m for m in marks
                      if (m.get("outcome_json") or {}).get("is_open")]
        marks = [m for m in marks
                 if not (m.get("outcome_json") or {}).get("is_open")]

        total_trades = len(marks)
        wins = [m for m in marks if float(m.get("profit") or 0.0) > 0]
        losses = [m for m in marks if float(m.get("profit") or 0.0) < 0]
        breakevens = [m for m in marks if float(m.get("profit") or 0.0) == 0]

        total_pnl = sum(float(m.get("profit") or 0.0) for m in marks)
        r_list = [float(m.get("r_multiple") or 0.0) for m in marks if m.get("r_multiple") is not None]
        avg_r = (sum(r_list) / len(r_list)) if r_list else 0.0

        win_r_list = [float(m.get("r_multiple") or 0.0) for m in wins if m.get("r_multiple") is not None]
        loss_r_list = [float(m.get("r_multiple") or 0.0) for m in losses if m.get("r_multiple") is not None]

        avg_win_r = (sum(win_r_list) / len(win_r_list)) if win_r_list else 0.0
        avg_loss_r = (sum(loss_r_list) / len(loss_r_list)) if loss_r_list else 0.0

        winrate = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0
        lossrate = (len(losses) / total_trades * 100) if total_trades > 0 else 0.0

        # Expectancy en R = (Win% * AvgWinR) - (Loss% * |AvgLossR|)
        expectancy_r = ((winrate / 100.0) * avg_win_r) - ((lossrate / 100.0) * abs(avg_loss_r))

        # Profit factor
        gross_profit = sum(float(m.get("profit") or 0.0) for m in wins)
        gross_loss = abs(sum(float(m.get("profit") or 0.0) for m in losses))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.9 if gross_profit > 0 else 0.0)

        # 1. Desglose por buckets de score
        buckets_map: Dict[str, Dict[str, Any]] = {
            "<60": {"trades": 0, "wins": 0, "losses": 0, "profit": 0.0, "r_sum": 0.0},
            "60-70": {"trades": 0, "wins": 0, "losses": 0, "profit": 0.0, "r_sum": 0.0},
            "70-80": {"trades": 0, "wins": 0, "losses": 0, "profit": 0.0, "r_sum": 0.0},
            "80+": {"trades": 0, "wins": 0, "losses": 0, "profit": 0.0, "r_sum": 0.0},
            "no_score": {"trades": 0, "wins": 0, "losses": 0, "profit": 0.0, "r_sum": 0.0},
        }

        # 2. Desglose por símbolo
        by_symbol: Dict[str, Dict[str, Any]] = {}

        # 3. Desglose por motivo de salida
        by_exit: Dict[str, int] = {}

        for m in marks:
            ctx = m.get("context_json") or {}
            score = ctx.get("score")
            b_key = self._bucket_score(score) if score is not None else "no_score"
            if b_key not in buckets_map:
                buckets_map[b_key] = {"trades": 0, "wins": 0, "losses": 0, "profit": 0.0, "r_sum": 0.0}

            pnl = float(m.get("profit") or 0.0)
            r_raw = m.get("r_multiple")
            r_val = float(r_raw) if r_raw is not None else None

            buckets_map[b_key]["trades"] += 1
            buckets_map[b_key]["profit"] += pnl
            if r_val is not None:
                buckets_map[b_key]["r_sum"] += r_val
                buckets_map[b_key]["r_n"] = buckets_map[b_key].get("r_n", 0) + 1
            if pnl > 0:
                buckets_map[b_key]["wins"] += 1
            elif pnl < 0:
                buckets_map[b_key]["losses"] += 1

            # Símbolo
            sym = str(m.get("symbol") or "UNKNOWN").upper()
            if sym not in by_symbol:
                by_symbol[sym] = {"trades": 0, "wins": 0, "losses": 0, "profit": 0.0, "r_sum": 0.0}
            by_symbol[sym]["trades"] += 1
            by_symbol[sym]["profit"] += pnl
            if r_val is not None:
                by_symbol[sym]["r_sum"] += r_val
                by_symbol[sym]["r_n"] = by_symbol[sym].get("r_n", 0) + 1
            if pnl > 0:
                by_symbol[sym]["wins"] += 1
            elif pnl < 0:
                by_symbol[sym]["losses"] += 1

            # Exit quality
            eq = str(m.get("exit_quality") or "UNKNOWN")
            by_exit[eq] = by_exit.get(eq, 0) + 1

        # Formatear buckets
        formatted_buckets = {}
        for k, v in buckets_map.items():
            cnt = v["trades"]
            rn = v.get("r_n", 0)
            wr = round((v["wins"] / cnt * 100), 1) if cnt > 0 else 0.0
            formatted_buckets[k] = {
                "trades": cnt,
                "wins": v["wins"],
                "losses": v["losses"],
                "winrate_pct": wr,
                "profit_usd": round(v["profit"], 2),
                "avg_r": round(v["r_sum"] / rn, 2) if rn > 0 else 0.0,
            }

        # Formatear by_symbol
        formatted_symbols = {}
        for sym, v in by_symbol.items():
            cnt = v["trades"]
            rn = v.get("r_n", 0)
            formatted_symbols[sym] = {
                "trades": cnt,
                "wins": v["wins"],
                "losses": v["losses"],
                "winrate_pct": round((v["wins"] / cnt * 100), 1) if cnt > 0 else 0.0,
                "profit_usd": round(v["profit"], 2),
                "avg_r": round(v["r_sum"] / rn, 2) if rn > 0 else 0.0,
            }

        # Motivos de rechazo de setup_log
        motivos_descarte: Dict[str, int] = {}
        total_evaluados = 0
        total_aprobados = 0
        registros_recientes = []

        try:
            setups = self._store.list_setup_log(limit=limit)
            for s in setups:
                total_evaluados += 1
                if s.get("validated"):
                    total_aprobados += 1
                else:
                    reasons = s.get("reject_reasons") or []
                    for r in reasons:
                        k = str(r).split(":")[0] if ":" in str(r) else str(r)[:35]
                        motivos_descarte[k] = motivos_descarte.get(k, 0) + 1

                registros_recientes.append({
                    "id": s.get("id"),
                    "symbol": s.get("symbol"),
                    "direction": s.get("direction"),
                    "verdict": s.get("verdict"),
                    "score": s.get("score"),
                    "entry": s.get("entry"),
                    "sl": s.get("sl"),
                    "target": s.get("target"),
                    "validated": bool(s.get("validated")),
                    "reasons": s.get("reject_reasons") or [],
                    "source": s.get("source"),
                    "timestamp": s.get("timestamp"),
                })
        except Exception:
            pass

        return {
            "summary": {
                "total_setups_evaluated": total_evaluados,
                "total_setups_approved": total_aprobados,
                "closed_trades_total": total_trades,
                "open_partial_trades": len(open_marks),
                "wins": len(wins),
                "losses": len(losses),
                "breakevens": len(breakevens),
                "winrate_pct": round(winrate, 1),
                "total_profit_usd": round(total_pnl, 2),
                "avg_r_multiple": round(avg_r, 2),
                "avg_win_r": round(avg_win_r, 2),
                "avg_loss_r": round(avg_loss_r, 2),
                "expectancy_r": round(expectancy_r, 2),
                "profit_factor": profit_factor,
            },
            "score_buckets": formatted_buckets,
            "by_symbol": formatted_symbols,
            "by_exit_reason": by_exit,
            "top_rejection_reasons": sorted(motivos_descarte.items(), key=lambda x: x[1], reverse=True)[:8],
            "recent_trade_marks": marks[:20],
            "open_trade_marks": open_marks[:20],
            "recent_activity": registros_recientes[:20],
        }
