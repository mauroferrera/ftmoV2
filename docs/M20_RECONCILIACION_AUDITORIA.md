# M20 — Reconciliación de Deals MT5 y Auditoría Post-Mortem (P0)

> **Estado:** Completada y Verificada  
> **Fecha:** 2026-11-01  
> **Decisión vinculada:** `D-037` en `.agent/DECISIONS.md`  
> **Suite de pruebas:** 761 tests pasando en verde  

---

## 1. El Problema Crítico Resuelto

Hasta esta fase, el proyecto contaba con la tabla `trade_marks` en SQLite (`database/models.py`) y métodos en `database/store.py`, pero **ningún servicio ni ruta escribía en ella al cerrarse las operaciones en el broker**. 

Consecuencias directas que cegaban el trading algorítmico:
- El endpoint `/api/audit/performance` devolvía siempre `closed_trades_total: 0`, `winrate_pct: 0` y `avg_r_multiple: 0.0`.
- Era imposible saber si el ratio R:R de 1.0R / 2.0R era rentable, si el filtro de score de 59.5 aportaba ventaja o si las salidas por SL se producían por ruido o por invalidación estructural.
- Sin telemetría real del broker, cualquier optimización de parámetros se hacía a ciegas.

---

## 2. Componentes Implementados

### A. Adaptador de Lectura de Historial (`adapters/mt5/mt5_forex.py`)
- Se implementó `MT5ForexAdapter.history_deals(days, position_id, ticket)` y `_fila_deal`.
- Consulta la función nativa `mt5.history_deals_get` de forma serializada y segura a través de `Session.call` dentro del hilo dedicado de MT5.
- Normaliza cada deal con: `ticket`, `order`, `time`, `type`, `entry`, `position_id`, `reason`, `volume`, `price`, `commission`, `swap`, `profit`, `symbol` y `comment`.
- Soporte en el doble de pruebas `tests/unit/mt5_fake.py` (`FakeDeal` y `history_deals_get`).

### B. Servicio de Reconciliación (`api/services/reconciliation.py`)
El servicio `ReconciliationService` procesa de forma determinista e idempotente los deals del historial de MT5:

1. **Emparejamiento por Posición (`position_id`):**
   - Agrupa deals por su identificador único de posición en MT5.
   - Separa deals de entrada (`entry == DEAL_ENTRY_IN = 0`) y deals de salida (`entry in (DEAL_ENTRY_OUT = 1, DEAL_ENTRY_OUT_BY = 3)`).
2. **Cómputo Financiero Agregado:**
   - Calcula el precio promedio ponderado de salida: $\bar{P}_{close} = \frac{\sum (P_{out} \times V_{out})}{\sum V_{out}}$
   - Suma el PnL neto real deduciendo comisiones completas de entrada y salida, swaps y tasas: $\text{PnL}_{net} = \text{Profit}_{raw} + \text{Commissions} + \text{Swaps} + \text{Fees}$
   - Mide la duración de la operación en segundos: $t_{close} - t_{open}$.
3. **Cálculo del R-Múltiplo Realizado:**
   - Si la orden tenía Stop Loss planificado (`sl > 0`):
     $$\text{Distancia de Riesgo} = |P_{open} - SL|$$
     $$R_{BUY} = \frac{\bar{P}_{close} - P_{open}}{\text{Distancia de Riesgo}}, \quad R_{SELL} = \frac{P_{open} - \bar{P}_{close}}{\text{Distancia de Riesgo}}$$
   - Si no hubo SL en precio pero hay riesgo monetario base ($125 en cuenta $25k): $R = \frac{\text{PnL}_{net}}{125.0}$.
4. **Clasificación Cualitativa de Salida (`exit_quality`):**
   - `TP`: Take Profit alcanzado (motivo 5 de MT5 o $R \ge 1.8$).
   - `SL`: Stop Loss golpeado con pérdida completa.
   - `BE`: Break-Even verificado (cierre en SL con $|R| \le 0.15$ o ganancia nula).
   - `TP_PARTIAL`: Parcial tomado a 1.0R.
   - `MANUAL_WIN` / `MANUAL_LOSS` / `MANUAL_BE`: Cerrado manualmente por el operador desde el terminal o teléfono.
5. **Cálculo de Excursiones Extremas (MAE / MFE):**
   - Extrae el mínimo y máximo de las velas entre apertura y cierre del trade para calcular MAE (Maximum Adverse Excursion) y MFE (Maximum Favorable Excursion) en múltiplos de R, diagnosticando si el SL fue rozado por spread antes de ir a favor.
6. **Vínculo con `setup_log` y Persistencia:**
   - Empareja con `setup_log` mediante `trade_result_json["order"] == position_id`.
   - Escribe en `trade_marks` con `store.save_trade_mark` (idempotente por clave única `ticket`).
   - Actualiza el estado de `setup_log` a `status: "CLOSED"` con su PnL y R definitivo.

---

## 3. Analítica y Métricas Cuantitativas (`/api/audit/performance`)

El endpoint `/api/audit/performance` ahora entrega métricas cuantitativas completas:

* **Expectancy en R:**
  $$E(R) = (\text{WinRate} \times \bar{R}_{win}) - (\text{LossRate} \times |\bar{R}_{loss}|)$$
* **Profit Factor:**
  $$\text{PF} = \frac{\sum \text{Ganancias}}{\sum |\text{Pérdidas}|}$$
* **Desglose por Buckets de Score:**
  - `<60` (operaciones forzadas o manuales)
  - `60-70` (calidad media)
  - `70-80` (alta confluencia)
  - `80+` (máxima convicción SMC + HTF + CME)
* **Desglose por Símbolo (`by_symbol`):**
  - Muestra volumen de operaciones, tasa de acierto, beneficio neto en USD y R promedio por cada par (EURUSD, XAUUSD, AUDNZD, etc.).
* **Desglose por Motivo de Salida (`by_exit_reason`):**
  - Conteo de salidas por `TP`, `SL`, `BE`, `TP_PARTIAL` y manuales.

---

## 4. Endpoints Disponibles

| Método | Endpoint | Descripción |
|---|---|---|
| `GET` | `/api/audit/performance` | Resumen cuantitativo, buckets de score, métricas por par y motivos de descarte. |
| `POST` | `/api/audit/reconcile?days=30` | Fuerza la reconciliación inmediata entre MT5 y SQLite. |
| `GET` | `/api/audit/marks?limit=100` | Listado de todas las marcas individuales registradas en `trade_marks`. |

---

## 5. Automatización en el AutoTrader

En `api/services/autotrader.py`, el método `run_cycle` invoca automáticamente `reconcile_closed_trades(days=7)` al final de cada iteración, garantizando que tan pronto como una posición se cierre en el broker (por TP, SL o trailing), se concilie de inmediato sin intervención del usuario.
