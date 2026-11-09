"""Servicio de Trading Autónomo (`AutoTraderService`).

Diseñado para operar en cuentas Demo / Evaluación con total libertad y disciplina:
1. Monitoreo en segundo plano de Killzones y estado de la cuenta.
2. Ciclo periódico de escaneo de setups SMC + HTF (Score >= 59.5).
3. Envío automático de órdenes a mercado respetando todas las puertas FTMO y riesgo.
4. Auto-gestión de posiciones abiertas:
   - Monitorea tickets con magic 8882026.
   - Al tocar 1:2R, cierra automáticamente el 50% de la posición (parcial).
   - Mueve el Stop Loss a Break-Even (precio de entrada + offset de seguridad).
   - Deja correr el 50% restante (runner) hacia el objetivo.
5. Bitácora de analítica de errores y aciertos para post-mortem continuo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import clock, paths, strategy

log = logging.getLogger("api.services.autotrader")

#: Cada cuánto se reconcilia el histórico. La reconciliación lee deals de 7
#: días y reescribe marcas: hacerlo cada 30 s era trabajo pesado de I/O de
#: bróker + base en CADA ciclo, y los deals cerrados no cambian tan rápido.
RECONCILE_EVERY_SEC = 300.0


class AutoTraderService:
    """Orquestador del bucle autónomo de trading y gestión de riesgo."""

    def __init__(
        self,
        runtime: Any = None,
        scan_interval_sec: int = 30,
        account_id: Optional[str] = None,
    ) -> None:
        self._rt = runtime
        self._interval = max(5, int(scan_interval_sec))
        self._account_id = account_id
        self._enabled = False
        self._task: Optional[asyncio.Task] = None
        self._last_scan_at: Optional[str] = None
        self._last_summary: Optional[Dict[str, Any]] = None
        self._executed_count = 0
        self._managed_count = 0
        self._last_error: Optional[str] = None
        self._last_rejection: Optional[str] = None
        self._symbol_cooldowns: Dict[str, float] = {}
        # `monotonic` y no `datetime`: mide INTERVALOS. Comparar sellos ISO con
        # el reloj de pared da saltos con un ajuste de hora del sistema.
        self._last_recon_at: Optional[float] = None
        self._last_recon_summary: Optional[Dict[str, Any]] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def account_id(self) -> Optional[str]:
        return self._account_id

    def _state_file_path(self) -> str:
        return os.path.join(paths.DATABASE_DIR, "autotrader_state.json")

    def should_autostart(self) -> bool:
        """Determina si el bot debe arrancar automáticamente al iniciar el runtime."""
        env_auto = os.getenv("AUTOTRADER_AUTOSTART", "").strip().lower() in ("1", "true", "yes", "on")
        if env_auto:
            return True
        p = self._state_file_path()
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return bool(data.get("enabled", False))
            except Exception:
                pass
        return False

    def _persist_state(self, enabled: bool) -> None:
        """Guarda el estado del AutoTrader en disco de forma atómica."""
        p = self._state_file_path()
        try:
            tmp = p + ".tmp"
            payload = {
                "enabled": enabled,
                "interval_sec": self._interval,
                "account_id": self._account_id,
                "updated_at": clock.now_iso(),
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, p)
        except Exception as exc:
            log.warning("No se pudo persistir estado de AutoTrader: %s", exc)

    def status(self) -> Dict[str, Any]:
        """Estado vigente del bot autónomo."""
        return {
            "enabled": self._enabled,
            "running": self._task is not None and not self._task.done(),
            "scan_interval_sec": self._interval,
            "account_id": self._account_id,
            "last_scan_at": self._last_scan_at,
            "executed_count": self._executed_count,
            "managed_count": self._managed_count,
            "last_error": self._last_error,
            "last_rejection": self._last_rejection,
            "last_reconcile_at": self._last_recon_summary,
            "last_summary": self._last_summary,
        }

    async def start(self) -> Dict[str, Any]:
        """Inicia el worker autónomo en segundo plano."""
        if self._enabled and self._task is not None and not self._task.done():
            return {"ok": True, "message": "AutoTrader ya está activo.", **self.status()}

        self._enabled = True
        self._last_error = None
        self._persist_state(True)
        self._task = asyncio.create_task(self._run_loop())
        log.info("AutoTrader iniciado en cuenta %s (intervalo %ss)", self._account_id, self._interval)
        return {"ok": True, "message": "AutoTrader activado.", **self.status()}

    async def stop(self, persist: bool = True) -> Dict[str, Any]:
        """Detiene el worker autónomo."""
        self._enabled = False
        if persist:
            self._persist_state(False)
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("AutoTrader detenido.")
        return {"ok": True, "message": "AutoTrader detenido.", **self.status()}

    async def _run_loop(self) -> None:
        """Bucle principal asíncrono que corre indefinidamente mientras enabled sea True."""
        log.info("Bucle AutoTrader arrancando...")
        while self._enabled:
            try:
                ahora = datetime.now(timezone.utc)
                self._last_scan_at = ahora.isoformat()
                res = await self.run_cycle(ahora=ahora)
                self._last_summary = res
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._last_error = str(exc)
                log.exception("Error en ciclo de AutoTrader: %s", exc)

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def run_cycle(self, ahora: Optional[datetime] = None) -> Dict[str, Any]:
        """Ejecuta un ciclo síncrono/asíncrono de evaluación y auto-gestión."""
        momento = ahora or datetime.now(timezone.utc)
        results = {
            "timestamp": momento.isoformat(),
            "positions_managed": [],
            "trades_executed": [],
            "rejected_orders": [],
            "skipped_reasons": [],
        }

        rt = self._rt
        if rt is None:
            results["skipped_reasons"].append("Runtime no disponible")
            return results

        # Preflight de MT5: validar permisos de trading algorítmico en la terminal
        market_svc = getattr(rt, "market", None)
        if market_svc is not None and hasattr(market_svc, "terminal_info"):
            try:
                ti = market_svc.terminal_info()
                if ti and not ti.get("trade_allowed", True):
                    self._last_error = "MT5 Algo Trading OFF: Habilita 'Algo Trading' en el terminal MetaTrader 5."
                    results["skipped_reasons"].append(self._last_error)
                    return results
            except Exception as exc:
                log.debug("Preflight terminal_info falló: %s", exc)

        # 1. Auto-Gestión de Posiciones Abiertas (Parciales a 2R y BE)
        #
        # Las tres piezas son síncronas y hablan con MT5 y con SQLite: en
        # `to_thread` no bloquean el event loop mientras el bróker responde
        # (antes tres llamadas bloqueantes por ciclo congelaban el resto de la
        # API justo cuando el dashboard necesita pintar).
        try:
            managed = await asyncio.to_thread(self._auto_manage_open_positions)
            results["positions_managed"] = managed
            self._managed_count += len(managed)
        except Exception as exc:
            log.warning("Fallo en auto-gestión de posiciones: %s", exc)
            results["skipped_reasons"].append(f"Auto-gestión error: {exc}")

        # 2. Evaluación de Nuevas Entradas (Auto-Execution)
        try:
            executed = await asyncio.to_thread(
                self._evaluate_and_execute_entries, momento)
            results["trades_executed"] = executed
            self._executed_count += len(executed)
        except Exception as exc:
            log.warning("Fallo en evaluación y auto-ejecución: %s", exc)
            results["skipped_reasons"].append(f"Auto-ejecución error: {exc}")

        # 3. Reconciliación de Deals Cerrados -> trade_marks (M20), con throttle:
        # cada 5 minutos, no cada ciclo. Es I/O de histórico + escritura.
        try:
            recon = getattr(rt, "reconciliation", None)
            if recon is not None:
                ahora_mono = time.monotonic()
                vencida = (self._last_recon_at is None
                           or ahora_mono - self._last_recon_at >= RECONCILE_EVERY_SEC)
                if vencida:
                    self._last_recon_summary = await asyncio.to_thread(
                        recon.reconcile_closed_trades, 7)
                    self._last_recon_at = ahora_mono
                    results["reconciliation"] = self._last_recon_summary
                else:
                    results["reconciliation"] = {
                        "status": "throttled",
                        "seconds_since_last": round(ahora_mono - self._last_recon_at, 1),
                        "every_sec": RECONCILE_EVERY_SEC,
                    }
        except Exception as exc:
            log.warning("Fallo en reconciliación de deals: %s", exc)

        return results

    def _exec_service(self) -> Optional[Any]:
        """Resuelve el ExecutionService con todas las puertas de riesgo y MT5."""
        rt = self._rt
        if rt is None:
            return None
        exec_attr = getattr(rt, "execution", None)
        if hasattr(exec_attr, "execute_market_trade") and hasattr(exec_attr, "management_view"):
            return exec_attr
        try:
            from api.services.execution import execution_from
            return execution_from(rt)
        except Exception as exc:
            log.warning("No se pudo construir ExecutionService para AutoTrader: %s", exc)
            return None

    def _magic_operado(self) -> Optional[int]:
        """El `magic` con el que opera el bot, de la config de ejecución.

        `None` cuando no se puede leer: entonces se listan todas las posiciones.
        Filtrar por magic es lo que separa "las operaciones de este bot" de
        "cualquier posición de la cuenta" (una manual, de otra estrategia…).
        """
        exec_svc = self._exec_service()
        lector = getattr(exec_svc, "_cfg", None)
        if not callable(lector):
            return None
        try:
            valor = int((lector() or {}).get("magic") or 0)
        except Exception:  # noqa: BLE001 - sin magic legible no se filtra
            return None
        return valor or None

    def _auto_manage_open_positions(self) -> List[Dict[str, Any]]:
        """Verifica cada posición abierta y aplica gestión automática a 2R si califica."""
        managed_list: List[Dict[str, Any]] = []
        exec_svc = self._exec_service()
        if exec_svc is None:
            return managed_list

        magic = self._magic_operado()
        try:
            if magic:
                pos_data, err = exec_svc.positions(magic=magic)
            else:
                pos_data, err = exec_svc.positions()
        except TypeError:
            # Puerto/doble sin el parámetro `magic`: se lista todo y se sigue.
            pos_data, err = exec_svc.positions()
        except Exception as exc:
            log.warning("No se pudieron leer posiciones en auto-gestión: %s", exc)
            return managed_list

        if err or not pos_data:
            return managed_list

        # Filtramos posiciones operadas por el bot (o todas las de la cuenta)
        market_svc = getattr(self._rt, "market", None) if self._rt is not None else None
        for p in pos_data:
            ticket = p.get("ticket")
            if not ticket:
                continue

            # Consultamos la vista de gestión
            try:
                raw_view = exec_svc.management_view(ticket)
                view = raw_view[0] if isinstance(raw_view, tuple) else raw_view
            except Exception as exc:
                log.debug("Sin vista de gestión para #%s: %s", ticket, exc)
                continue

            if not isinstance(view, dict):
                continue

            can_apply = bool(view.get("can_apply"))
            force_be = False
            exhaustion_reason: Optional[str] = None

            # Regla 3 (Salida Inteligente por Agotamiento de Volumen o Equilibrio 50%):
            # Si la posición va en ganancia pero aún no toca 1R/2R, revisamos si llegó al 50%
            # del rango diario (Equilibrium) o si el volumen de TradingView se secó / giró en contra.
            if not can_apply and not view.get("be_applied") and view.get("r_original"):
                r_act = float(view.get("r_actual") or 0.0)
                profit_usd = float(p.get("profit") or 0.0)
                sym_pos = str(view.get("symbol") or p.get("symbol") or "").upper()
                dir_pos = str(view.get("direction") or "").upper()
                cur_p = float(p.get("price_current") or 0.0)

                if r_act >= 0.35 or profit_usd >= 25.0:
                    # A) Revisar llegada al 50% Equilibrium del rango diario (PDH/PDL)
                    if market_svc is not None and cur_p > 0:
                        try:
                            pat_d = market_svc.pattern_data(sym_pos, "M15", 60)
                            if isinstance(pat_d, dict):
                                pdh_v = pat_d.get("pdh")
                                pdl_v = pat_d.get("pdl")
                                if pdh_v and pdl_v and float(pdh_v) > float(pdl_v):
                                    eq_50 = (float(pdh_v) + float(pdl_v)) / 2.0
                                    if (dir_pos == "BUY" and cur_p >= eq_50) or (dir_pos == "SELL" and cur_p <= eq_50):
                                        force_be = True
                                        exhaustion_reason = f"precio alcanzó Equilibrio 50% diario ({eq_50:.5f})"
                        except Exception:
                            pass

                    # B) Revisar agotamiento de volumen o absorción contraria en TradingView
                    if not force_be and sym_pos:
                        try:
                            from adapters.tradingview.tv_adapter import CME_FUTURES_MAP, TradingViewAdapter
                            if sym_pos in CME_FUTURES_MAP:
                                ic = TradingViewAdapter().evaluate_confluence(sym_pos, dir_pos, timeframe="M15")
                                ic_st = str((ic or {}).get("status") or "")
                                if ic_st in ("BAJO_VOLUMEN_CME", "BAJO_VOLUMEN_SPOT", "ALERTA_CONTRA_BALLENAS"):
                                    force_be = True
                                    exhaustion_reason = f"agotamiento/giro de volumen en TradingView ({ic_st})"
                        except Exception:
                            pass

            if not can_apply and not force_be:
                continue

            if force_be:
                log.info("Posición #%s (%s %s) con ganancia (+%.2fR): protegiendo en Break-Even por %s...",
                         ticket, view.get("symbol"), view.get("direction"),
                         view.get("r_actual") or 0.0, exhaustion_reason)
            else:
                log.info("Posición #%s (%s %s) lista para gestión a %.1fR. Aplicando...",
                         ticket, view.get("symbol"), view.get("direction"), view.get("r_actual") or 0.0)

            try:
                res, code = exec_svc.apply_management(ticket, force=force_be)
                if code == 200:
                    managed_list.append({
                        "ticket": ticket,
                        "symbol": view.get("symbol"),
                        "r_actual": view.get("r_actual"),
                        "partial_volume": res.get("partial_volume"),
                        "be_level": res.get("be_level"),
                        "reason": exhaustion_reason or "target_r_reached",
                    })
                    log.info("Gestión exitosa en #%s: parcial=%s, BE=%s",
                             ticket, res.get("partial_volume"), res.get("be_level"))
                else:
                    log.warning("Rechazo al aplicar gestión a #%s: %s (código %s)", ticket, res, code)
            except Exception as exc:
                log.error("Error al aplicar gestión a #%s: %s", ticket, exc)

        return managed_list

    def _check_triple_confluence(
        self,
        symbol: str,
        direction: str,
        planned_entry: Optional[float],
        current_price: Optional[float],
        momento: datetime,
        killzone_info: Optional[Any] = None,
        pdh: Optional[float] = None,
        pdl: Optional[float] = None,
        inst_confluence: Optional[Dict[str, Any]] = None,
        continuity_mode: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """Aplica el Filtro de Confluencia Institucional + Estrategia del Usuario:
        
        1. Ventana de Killzone activa (Londres 07:00-10:00 UTC o NY 12:30-15:30 UTC).
        2. Tribunal Macro y Consenso Web / Bancos (EURUSD) + Matriz HTF (Continuidad vs Giro).
        3. Regla 1 (Premium vs. Discount + Excepción de Continuidad por Noticias/HTF en Retroceso OB/OTE).
        4. Regla 2 y 4 (Volumen VSA TradingView + Brújula Dólar DXY): Fuerza de volumen / absorción y sin SMT opuesto.
        5. Mitigación / Toque de la zona (OB/FVG/OTE): no perseguir velas lejanas.
        """
        # 1. Ventana de Killzone
        in_kz = False
        if isinstance(killzone_info, dict):
            in_kz = bool(killzone_info.get("in_killzone"))
        elif isinstance(killzone_info, str) and killzone_info:
            in_kz = True

        if not in_kz:
            from core import risk_engine
            kz_res = risk_engine.killzone_score(now=momento)
            in_kz = bool(kz_res.get("in_killzone"))

        if not in_kz:
            return False, "Fuera de Killzone (requiere Londres 07-10 UTC o NY 12:30-15:30 UTC)"

        sym_clean = str(symbol or "").upper().replace("/", "")
        has_continuity = bool(continuity_mode)

        # 2. Tribunal Macro y Consenso Web / Bancos (EURUSD)
        if sym_clean == "EURUSD" and self._rt is not None:
            try:
                import json
                from api.services.eurusd_spot_analysis_service import EurUsdSpotAnalysisService
                spot = EurUsdSpotAnalysisService().get_latest()
                if spot:
                    payload = json.loads(spot.get("payload_json") or "{}")
                    trib = payload.get("tribunal") if isinstance(payload, dict) else {}
                    t_verdict = trib.get("verdict") if isinstance(trib, dict) and isinstance(trib.get("verdict"), dict) else {}
                    v_status = str(t_verdict.get("status") or "").upper()
                    if v_status == "VETO":
                        return False, "VETO del Tribunal Macro EUR/USD (evento o alto riesgo)"

                    b_bull = int(spot.get("consensus_bull") or 0)
                    b_bear = int(spot.get("consensus_bear") or 0)
                    b_neut = int(spot.get("consensus_neutral") or 0)
                    total_sources = b_bull + b_bear + b_neut
                    if direction == "BUY" and total_sources > 0 and b_bear > b_bull:
                        return False, f"Consenso Web mayoritariamente bajista ({b_bear} vs {b_bull} bull)"
                    if direction == "SELL" and total_sources > 0 and b_bull > b_bear:
                        return False, f"Consenso Web mayoritariamente alcista ({b_bull} vs {b_bear} bear)"

                    macro_bias = str(spot.get("bias") or "").lower()
                    macro_conf = float(spot.get("confidence") or 0.0)
                    if direction == "BUY" and macro_bias == "bajista":
                        return False, "Tribunal Macro con sesgo bajista opuesto a BUY"
                    if direction == "SELL" and macro_bias == "alcista":
                        return False, "Tribunal Macro con sesgo alcista opuesto a SELL"
                    if macro_conf >= 60.0 and (
                        (direction == "BUY" and macro_bias == "alcista")
                        or (direction == "SELL" and macro_bias == "bajista")
                    ):
                        has_continuity = True
            except Exception as exc:
                log.debug("No se pudo evaluar confluencia macro para %s: %s", symbol, exc)

        # 3. Ubicación Premium vs. Discount + Excepción de Continuidad por Tendencia HTF / Noticias
        if (pdh is None or pdl is None) and self._rt is not None:
            market_svc = getattr(self._rt, "market", None)
            if market_svc is not None and hasattr(market_svc, "pattern_data"):
                try:
                    p_data = market_svc.pattern_data(sym_clean, "M15", 60)
                    if isinstance(p_data, dict):
                        pdh = p_data.get("pdh")
                        pdl = p_data.get("pdl")
                        htf_ctx = (p_data.get("analysis") or {}).get("htf_context")
                        if not has_continuity and isinstance(htf_ctx, dict) and htf_ctx.get("timeframes"):
                            from core import smc_engine
                            hc = smc_engine.evaluate_htf_confluence(htf_ctx, direction=direction, current_price=current_price)
                            if float(hc.get("score_bonus") or 0.0) >= 4.0:
                                has_continuity = True
                except Exception:
                    pass

        if pdh is not None and pdl is not None and float(pdh) > float(pdl) and current_price is not None:
            pdh_f, pdl_f, cp_f = float(pdh), float(pdl), float(current_price)
            eq_50 = (pdh_f + pdl_f) / 2.0
            pos_pct = ((cp_f - pdl_f) / (pdh_f - pdl_f)) * 100.0
            if direction == "SELL" and cp_f <= eq_50 and not (has_continuity and planned_entry is not None):
                return False, f"Precio en Zona Barata / Discount ({pos_pct:.0f}% del rango diario): sin catalizador de continuidad, prohibido vender en el piso"
            if direction == "BUY" and cp_f > eq_50 and not (has_continuity and planned_entry is not None):
                return False, f"Precio en Zona Cara / Premium ({pos_pct:.0f}% del rango diario): sin catalizador de continuidad, prohibido comprar en el techo"

        # 4. Regla 2 (Volumen VSA TradingView) + Regla 4 (Brújula Dólar DXY)
        if inst_confluence is None and self._rt is not None:
            try:
                from adapters.tradingview.tv_adapter import CME_FUTURES_MAP, TradingViewAdapter
                if sym_clean in CME_FUTURES_MAP:
                    inst_confluence = TradingViewAdapter().evaluate_confluence(sym_clean, direction, timeframe="M15")
            except Exception:
                inst_confluence = None

        if isinstance(inst_confluence, dict):
            ic_status = str(inst_confluence.get("status") or "")
            ic_ratio = inst_confluence.get("vol_ratio")
            ic_abs = inst_confluence.get("absorption")
            smt_status = str(inst_confluence.get("smt_status") or "")
            if ic_status in ("BAJO_VOLUMEN_CME", "BAJO_VOLUMEN_SPOT"):
                return False, f"Volumen débil en TradingView ({ic_ratio}x): esperando pico de volumen o absorción"
            if ic_status == "ALERTA_CONTRA_BALLENAS":
                return False, f"Absorción institucional en contra ({ic_abs}) en TradingView"
            if (
                (direction == "BUY" and smt_status == "BEARISH_SMT")
                or (direction == "SELL" and smt_status == "BULLISH_SMT")
            ) and ic_status != "ALTA_CONFLUENCIA_BALLENAS":
                return False, f"Brújula Dólar (DXY) en contra ({smt_status})"

        # 5. Mitigación / Toque de la zona (no perseguir precio)
        if planned_entry is not None and current_price is not None:
            pe = float(planned_entry)
            cp = float(current_price)
            is_jpy = "JPY" in sym_clean
            pip_size = 0.01 if is_jpy else 0.00010
            max_drift_pips = 3.5

            if direction == "BUY":
                drift_pips = (cp - pe) / pip_size
                if drift_pips > max_drift_pips:
                    return False, f"Precio alejado de la zona (+{drift_pips:.1f} pips). Esperando retroceso a OB/OTE"
            elif direction == "SELL":
                drift_pips = (pe - cp) / pip_size
                if drift_pips > max_drift_pips:
                    return False, f"Precio alejado de la zona (-{drift_pips:.1f} pips). Esperando retroceso a OB/OTE"

        return True, None

    def _evaluate_and_execute_entries(self, momento: datetime) -> List[Dict[str, Any]]:
        """Escanea setups en M15. Si están aprobados por el gate, envía orden a mercado."""
        executed: List[Dict[str, Any]] = []
        watcher_svc = getattr(self._rt, "watcher", None)
        exec_svc = self._exec_service()
        market_svc = getattr(self._rt, "market", None)

        if not watcher_svc or not exec_svc or not market_svc:
            return executed

        # Ejecutamos el escaneo normal del watcher para auditar en setup_log
        scan_res = watcher_svc.escanear(ahora=momento, force_killzone=False)
        events = scan_res.get("events") or []

        # Buscamos eventos de setup nuevos o activos aprobados
        candidatos: List[Dict[str, Any]] = [
            ev for ev in events if ev.get("event") == "new"
        ]

        # También consideramos estados que ya estén 'active' en setup_state
        # y que no tengan ya una posición abierta (ej. bot encendido tras detectarse el setup)
        store = getattr(self._rt, "store", None)
        if store is not None:
            try:
                pos_abiertas = set()
                try:
                    pos_data, _ = exec_svc.positions()
                    if pos_data:
                        pos_abiertas = {p.get("symbol") for p in pos_data if p.get("symbol")}
                except Exception:
                    pass

                from database import store as db_store
                for est in db_store.list_setup_states():
                    sym = est.get("symbol")
                    if not sym or sym in pos_abiertas:
                        continue
                    if any(c.get("symbol") == sym for c in candidatos):
                        continue
                    if est.get("status") == "active":
                        candidatos.append({
                            "symbol": sym,
                            "direction": est.get("direction"),
                            "score": float(est.get("score") or 0.0),
                            "verdict": est.get("verdict"),
                            "entry": est.get("entry"),
                            "sl": est.get("sl"),
                            "tp": est.get("target"),
                            "invalidate_level": est.get("invalidate_level"),
                            "created_at": est.get("updated_at") or est.get("created_at") or momento.isoformat(),
                        })
            except Exception as exc:
                log.debug("No se pudieron consultar estados activos adicionales: %s", exc)

        cfg_exec = getattr(exec_svc, "_cfg", None)
        cfg = cfg_exec() if callable(cfg_exec) else {}
        use_triple_confluence = bool(cfg.get("autotrader_triple_confluence", False))

        now_mono = time.monotonic()
        for ev in candidatos:
            symbol = ev.get("symbol")
            tf = ev.get("timeframe") or "M15"
            direction = (ev.get("direction") or "").upper()
            score = float(ev.get("score") or 0.0)

            if direction not in ("BUY", "SELL"):
                continue

            # Backoff por símbolo: si fue rechazado recientemente, no bombardear a MT5
            cd = self._symbol_cooldowns.get(symbol, 0.0)
            if now_mono < cd:
                log.debug("Símbolo %s en cooldown tras rechazo previo (%ds restantes)", symbol, int(cd - now_mono))
                continue

            # Filtro de Triple Confluencia (Web Consensus + Tribunal no VETO + Killzone + Mitigación de Zona)
            if use_triple_confluence and not ev.get("force"):
                current_p = None
                try:
                    p_info = market_svc.price(symbol)
                    if p_info:
                        current_p = p_info.get("ask") if direction == "BUY" else p_info.get("bid")
                except Exception:
                    pass

                passed, rej_reason = self._check_triple_confluence(
                    symbol=symbol,
                    direction=direction,
                    planned_entry=ev.get("entry"),
                    current_price=current_p,
                    momento=momento,
                    killzone_info=ev.get("killzone"),
                )
                if not passed:
                    self._last_rejection = f"[{symbol}] {rej_reason}"
                    log.info("AutoTrader descarta setup: %s", self._last_rejection)
                    continue

            log.info("Setup APROBADO detectado por AutoTrader: %s %s (Score %.1f)", symbol, direction, score)

            # Enviamos la orden mediante ExecutionService pasando por todas las puertas FTMO
            # Dejamos volume=None para que se calcule el lote automático exacto según el riesgo 0.5%
            cuerpo_orden = {
                "symbol": symbol,
                "action": direction,
                "volume": None,
                "sl": ev.get("sl"),
                "tp": ev.get("tp"),
                "created_at": ev.get("created_at") or momento.isoformat(),
                "planned_entry": ev.get("entry"),
                "invalidate_level": ev.get("invalidate_level"),
                "score": score,
                "verdict": ev.get("verdict"),
                "force": False,
            }

            try:
                res, status_code = exec_svc.execute_market_trade(
                    symbol=symbol,
                    action=direction,
                    volume=None,
                    sl=cuerpo_orden["sl"],
                    tp=cuerpo_orden["tp"],
                    force=False,
                    source="autotrader",
                    created_at=cuerpo_orden["created_at"],
                    planned_entry=cuerpo_orden["planned_entry"],
                    invalidate_level=cuerpo_orden["invalidate_level"],
                    score=cuerpo_orden["score"],
                    verdict=cuerpo_orden["verdict"],
                )

                if status_code == 200:
                    ticket = res.get("position_ticket") or res.get("ticket")
                    lote = res.get("volume")
                    precio_lleno = res.get("fill_price") or res.get("price")
                    log.info("ORDEN AUTÓNOMA EJECUTADA: #%s %s %s %.2f lotes @ %s",
                             ticket, symbol, direction, lote or 0.0, precio_lleno)
                    executed.append({
                        "ticket": ticket,
                        "symbol": symbol,
                        "action": direction,
                        "volume": lote,
                        "price": precio_lleno,
                        "sl": res.get("sl"),
                        "tp": res.get("tp"),
                        "score": score,
                    })
                    self._last_rejection = None
                    self._symbol_cooldowns.pop(symbol, None)
                else:
                    err_msg = str(res.get("error") or res.get("detail") or res)
                    retcode = res.get("retcode")
                    self._last_rejection = f"[{symbol}] {err_msg}"
                    # Backoff de 3 minutos tras un rechazo para este símbolo
                    self._symbol_cooldowns[symbol] = now_mono + 180.0
                    if retcode == 10027 or "Algo Trading" in err_msg or "deshabilitado" in err_msg:
                        self._last_error = f"MT5 Algo Trading OFF: {err_msg}"
                    log.warning("Orden rechazada en %s (Status %s): %s", symbol, status_code, err_msg)
            except Exception as exc:
                self._last_rejection = f"[{symbol}] Error: {exc}"
                self._symbol_cooldowns[symbol] = now_mono + 180.0
                log.error("Excepción al ejecutar trade autónomo en %s: %s", symbol, exc)

        return executed

