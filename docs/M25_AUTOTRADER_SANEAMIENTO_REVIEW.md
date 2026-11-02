# M25 · Saneamiento y Robustecimiento Post-Review del Autotrader y Ejecución (D-042)

## 1. Motivación y Diagnóstico
Tras la implementación de los hitos M20 a M24, una auditoría técnica profunda del pipeline operativo identificó 9 hallazgos críticos (6 de severidad P0 y 3 P1) que comprometían la ejecución en vivo:

1. **R:R vs Deslizamiento (P0):** Al enviar órdenes con el plan institucional aprobado (2R), cualquier micro-deslizamiento adverso del precio vivo rompía la validación estricta $(TP - entry) / (entry - SL) \ge 2.0$. Zonas con deriva permitida de $0.2 \cdot SL$ caían a R:R de 1.50 y eran rechazadas por el gate de ejecución (`GATE_REJECTED`), impidiendo operar tanto en modo autónomo como manual.
2. **Pisado del TP de Imán en Gestión a 1R (P0):** Al alcanzar 1R y tomar parciales, `ExecutionService.apply_management` sobreescribía ciegamente el TP con `runner_level` (2R), retrayendo objetivos legítimos anclados a imanes de liquidez mayores (p. ej. 3.5R).
3. **Reconciliación Desincronizada (P0):** `reconciliation.py` indexaba `setup_log` usando `order` (ticket de orden de envío) en lugar de `position_ticket` (ticket de la posición en MT5). En netting o ejecuciones múltiples, los tickets diferían y la reconciliación caía al emparejamiento difuso por ventana temporal ($\pm 300$ s).
4. **Denominador Errático en R Realizado (P0):** Si una posición llegaba reconciliada con `be_level` establecido, el cálculo de riesgo inicial usaba $|entry - be\_level|$ en lugar de la distancia de stop loss original (`r_original`), generando R-múltiplos astronómicos espurios.
5. **Imán de Liquidez sin Cablear y Spread Price Ausente (P0):** `MarketService.snapshot()` no publicaba `spread_price`, y `setup_gate.py` no conectaba el imán detectado por el motor HTF (`htf_conf["next_liquidity"]`) a la función `structural_sl_and_tp`.
6. **Redondeo Asimétrico y Micro-Tolerancia de R:R (P0):** Redondear independientemente entrada, SL y TP generaba en ocasiones R:R de 1.999999 por precisión de coma flotante, rechazando setups válidos.
7. **Clusters Macro Bloqueantes (P1):** Un límite plano por divisa (`max_positions_per_cluster: 1`) impedía abrir EURUSD si ya existía AUDUSD abierto, aunque ambas posiciones fuesen complementarias o no sobreexpusieran la misma dirección de divisa.
8. **Transparencia en Distancia de Stop y Piso ATR (P1):** `is_structural` se marcaba como `True` aun cuando el stop fuese fijado por el mínimo de configuración (`sl_min`). Se requería un piso de volatilidad real (`atr_min_mult`) y publicación honesta de la fuente ganadora (`dist_source`).
9. **Bloqueo del Event Loop y Endpoint Desprotegido (P1):** La reconciliación de deals leía 7 días de historial en cada ciclo (30 s) sincrónicamente en el hilo principal de asyncio, congelando la API. Además, `POST /api/audit/reconcile` carecía de autenticación de token.

---

## 2. Soluciones Implementadas

### A. Validación de Tesis contra `planned_entry` y Tolerancia de 1 Tick (`core/risk_engine.py`)
- La función pura `validate_entry` ahora valida la relación Beneficio/Riesgo (R:R) contra la entrada planificada del setup (`rr_entry`), mientras que la calidad de ejecución (`execution_quality`) continúa controlando estrictamente la deriva admisible del precio vivo ($\le 0.5 \cdot SL$).
- Se añadió un parámetro `tick_size` y una tolerancia de 1 tick para evitar rechazos por redondeo de punto flotante en brókers con precios fraccionarios.

### B. Ratchet de Take Profit (`core/exit_policy.py`)
- Se implementó y exportó la función pura `ratchet_tp(current_tp, target_tp, direction)`.
- En compras (BUY), el TP solo puede subir o mantenerse (`max`); en ventas (SELL), solo puede descender o mantenerse (`min`). Los objetivos ampliados a imanes de liquidez quedan protegidos contra contracciones al mover a Break-Even.

### C. Reconciliación Determinista y Denominador `r_original` (`api/services/execution.py` y `reconciliation.py`)
- `ExecutionService.execute_market_trade` resuelve el ticket real de posición (`_ticket_de_posicion`) a partir de la respuesta del bróker y lo persiste en `setup_log` como `position_ticket`.
- `reconciliation.py` indexa y empareja primeramente por `position_ticket`.
- El denominador para el cómputo de $R$ utiliza invariablemente `r_original = abs(entry_price - sl_original)` extraído del plan inicial del setup.

### D. Imán de Liquidez HTF y `spread_price` (`core/setup_gate.py` y `api/services/market.py`)
- `MarketService.snapshot()` y el helper `spread_price(symbol)` exponen el spread en unidades de precio.
- `setup_gate.py` inyecta automáticamente el precio de `htf_conf["next_liquidity"]["price"]` como `target_magnet` en `structural_sl_and_tp`.

### E. Factores Direccionales y Piso ATR Honesto (`core/clusters.py` e `indicators.py`)
- `core/clusters.py` clasifica la exposición en factores direccionales (`USD_long`, `USD_short`, `JPY_long`, etc.). Se ajustó el límite a 2 posiciones por factor macro, manteniendo el límite agregado de riesgo vivo total en 1.5R.
- `core/indicators.py` incorpora `atr_min_mult` (piso de volatilidad por defecto 1.0 ATR), deriva distancias redondeadas de forma atómica y publica `dist_source` (`"structure"`, `"atr"`, `"config"`, `"fallback"`) con `is_structural = True` exclusivamente cuando la estructura técnica es la ganadora.

### F. Throttling de Reconciliación y Seguridad de API (`autotrader.py` y `audit.py`)
- `AutoTraderService` ejecuta la reconciliación periódica cada 300 segundos (5 minutos) utilizando `time.monotonic()` para evitar afectaciones por ajustes de reloj, y delega el I/O a `asyncio.to_thread`.
- Filtra posiciones operadas por el `magic` configurado.
- `POST /api/audit/reconcile` exige cabecera de autenticación `X-API-Token` mediante `deps.require_api_token`.

---

## 3. Verificación
- **Pureza de Núcleo:** `tests/unit/test_core_purity.py` — 78/78 tests pasados con cero dependencias de MT5, pandas o librerías impuras en `core/`.
- **Suite Completa:** 805 tests pasados en verde (`pytest -q` / `pytest tests --tb=short`).
- **Control de Regresión:** Sin fallas de integración en rutas de auditoría, ejecución ni cálculo de indicadores.

