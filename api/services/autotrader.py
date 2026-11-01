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
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import clock, strategy

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
            "last_reconcile_at": self._last_recon_summary,
            "last_summary": self._last_summary,
        }

    async def start(self) -> Dict[str, Any]:
        """Inicia el worker autónomo en segundo plano."""
        if self._enabled and self._task is not None and not self._task.done():
            return {"ok": True, "message": "AutoTrader ya está activo.", **self.status()}

        self._enabled = True
        self._last_error = None
        self._task = asyncio.create_task(self._run_loop())
        log.info("AutoTrader iniciado en cuenta %s (intervalo %ss)", self._account_id, self._interval)
        return {"ok": True, "message": "AutoTrader activado.", **self.status()}

    async def stop(self) -> Dict[str, Any]:
        """Detiene el worker autónomo."""
        self._enabled = False
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
            "skipped_reasons": [],
        }

        rt = self._rt
        if rt is None:
            results["skipped_reasons"].append("Runtime no disponible")
            return results

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

    def _magic_operado(self) -> Optional[int]:
        """El `magic` con el que opera el bot, de la config de ejecución.

        `None` cuando no se puede leer: entonces se listan todas las posiciones.
        Filtrar por magic es lo que separa "las operaciones de este bot" de
        "cualquier posición de la cuenta" (una manual, de otra estrategia…).
        """
        exec_svc = getattr(self._rt, "execution", None)
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
        exec_svc = getattr(self._rt, "execution", None)
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
        for p in pos_data:
            ticket = p.get("ticket")
            if not ticket:
                continue

            # Consultamos la vista de gestión
            try:
                view = exec_svc.management_view(ticket)
            except Exception as exc:
                log.debug("Sin vista de gestión para #%s: %s", ticket, exc)
                continue

            if not view or not view.get("can_apply"):
                continue

            # Califica para gestión (ej. superó 2R y no ha tomado parcial o movido a BE)
            log.info("Posición #%s (%s %s) lista para gestión a %.1fR. Aplicando...",
                     ticket, view.get("symbol"), view.get("direction"), view.get("r_actual") or 0.0)

            try:
                res, code = exec_svc.apply_management(ticket, force=False)
                if code == 200:
                    managed_list.append({
                        "ticket": ticket,
                        "symbol": view.get("symbol"),
                        "r_actual": view.get("r_actual"),
                        "partial_volume": res.get("partial_volume"),
                        "be_level": res.get("be_level"),
                    })
                    log.info("Gestión exitosa en #%s: parcial=%s, BE=%s",
                             ticket, res.get("partial_volume"), res.get("be_level"))
                else:
                    log.warning("Rechazo al aplicar gestión a #%s: %s (código %s)", ticket, res, code)
            except Exception as exc:
                log.error("Error al aplicar gestión a #%s: %s", ticket, exc)

        return managed_list

    def _evaluate_and_execute_entries(self, momento: datetime) -> List[Dict[str, Any]]:
        """Escanea setups en M15. Si están aprobados por el gate, envía orden a mercado."""
        executed: List[Dict[str, Any]] = []
        watcher_svc = getattr(self._rt, "watcher", None)
        exec_svc = getattr(self._rt, "execution", None)
        market_svc = getattr(self._rt, "market", None)

        if not watcher_svc or not exec_svc or not market_svc:
            return executed

        # Ejecutamos el escaneo normal del watcher para auditar en setup_log
        scan_res = watcher_svc.escanear(ahora=momento, force_killzone=False)
        events = scan_res.get("events") or []

        # Buscamos eventos de setup nuevos o activos que estén aprobados
        for ev in events:
            # Solo eventos 'new' aprobados con dirección válida
            if ev.get("event") != "new":
                continue

            symbol = ev.get("symbol")
            tf = ev.get("timeframe") or "M15"
            direction = (ev.get("direction") or "").upper()
            score = float(ev.get("score") or 0.0)

            if direction not in ("BUY", "SELL"):
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
                "created_at": momento.isoformat(),
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
                    # `position_ticket` es el ticket de POSICIÓN; `ticket` se
                    # mantiene como reserva para puertos/dobles antiguos que no
                    # lo publican (un `order` no sirve para gestionar).
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
                else:
                    log.warning("Orden rechazada por puertas de seguridad: %s (Status %s)", res, status_code)
            except Exception as exc:
                log.error("Excepción al ejecutar trade autónomo en %s: %s", symbol, exc)

        return executed

