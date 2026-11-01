# M21 — Stop Loss Estructural, Volatilidad ATR(14) y Dimensionamiento Real (P0)

> **Estado:** Completada y Verificada  
> **Fecha:** 2026-11-01  
> **Decisión vinculada:** `D-038` en `.agent/DECISIONS.md`  
> **Suite de pruebas:** 769 tests pasando en verde  

---

## 1. El Problema Crítico Resuelto

Hasta esta fase, el sistema adolecía de dos graves inconsistencias estratégicas señaladas en la auditoría técnica institucional:

1. **Stop Loss Desconectado de la Estructura:**
   * En `core/setup_gate.py` se identificaba la zona institucional distal, pero al disparar a mercado `ExecutionService.execute_market_trade` recalculaba el SL a una distancia fija en pips desde el precio vivo (`sl = precio_vivo ± sl_distance`).
   * El SL real de la orden no coincidía con el plan del gráfico y podía quedar dentro de la zona o en un nivel incoherente.
2. **Cero Volatilidad (Ausencia de ATR):**
   * No existía ATR en ningún módulo. Un stop fijo por símbolo (XAUUSD $8.0, EURUSD 12 pips) era idéntico en la calma asiática que en la apertura de New York o durante noticias de impacto, provocando que los stops fueran barridos por mero ruido de mercado.
3. **Incoherencia de Umbrales entre Gate y Ejecución:**
   * `ENTRY_ZONE_MAX_SPAN_SL_MULT = 3.0` permitía aprobar setups cuya zona distaba hasta 3.0 veces el SL, pero luego `execution_quality` rechazaba la orden por deriva adversa (`max_slippage_sl_share = 0.5`). Zonas entre 0.5 y 3.0 SL pasaban la primera puerta y morían en la segunda.

---

## 2. Componentes Implementados

### A. Indicador ATR(14) Puro (`core/indicators.py`)
* Implementación matemática del **Average True Range (Wilder)** sobre velas normalizadas:
  $$TR_t = \max(H_t - L_t, |H_t - C_{t-1}|, |L_t - C_{t-1}|)$$
  $$\text{ATR}_t = \frac{\text{ATR}_{t-1} \times (period - 1) + TR_t}{period}$$
* 100% puro (sin I/O, sin red, sin MT5), verificado bajo `tests/unit/test_core_purity.py`.
* Integrado en `core/smc_engine.py` (`analyze()`) y publicado automáticamente en cada snapshot de mercado.

### B. Cálculo de SL Estructural con Colchón Dinámico (`structural_sl_and_tp`)
* **Anclaje al Distal Institucional:**
  * En compras (BUY): el stop loss se coloca estrictamente **por debajo** del borde distal (`bottom`) de la zona SMC.
  * En ventas (SELL): el stop loss se coloca estrictamente **por encima** del borde distal (`top`) de la zona SMC.
* **Colchón de Volatilidad Antiruido:**
  $$\text{buffer} = \max(0.15 \times \text{ATR}_{14}, 2.0 \times \text{spread}, 1.0 \times \text{tick\_size})$$
  $$\text{SL}_{raw} = \text{Distal} \mp \text{buffer}$$
* **Piso Mínimo (`sl_min`):**
  * La distancia configurada en `strategy.yaml` (`sl_distance`) pasa a actuar como **distancia mínima de seguridad** (`sl_min`), impidiendo stops ridículamente estrechos de 1 pip que morirían en el spread:
  $$\text{Distancia Real} = \max(|\text{Entry} - \text{SL}_{raw}|, \text{sl\_min})$$
  $$\text{SL} = \text{Entry} \mp \text{Distancia Real}$$
* **Take Profit al Imán de Liquidez:**
  * Si existe un imán institucional mayor (`htf_context.liquidity_magnet`: `PDH`, `PDL`, swing mayor) y ofrece al menos $2.0R$ ($\ge 2 \times \text{Distancia Real}$), el TP se ancla automáticamente al imán; de lo contrario, se proyecta a $2.0R$ directo.

### C. Coherencia Matemática de Puertas
* Se ajustó `ENTRY_ZONE_MAX_SPAN_SL_MULT = 0.5` en `core/setup_gate.py`.
* Cualquier zona que quede a más de $0.5 \times SL$ del precio actual es desestimada de inmediato, asegurando que ningún setup aprobado sea rechazado por deriva de deslizamiento en `execution_quality`.

### D. Ejecución y Dimensionamiento Real de Lote
* En `api/services/execution.py` y `api/routes/execution.py`:
  * `execute_market_trade` y `TradeMarketBody` aceptan ahora niveles absolutos `sl` y `tp`.
  * Si se proporciona `sl`, el servicio valida que esté del lado correcto y calcula `sl_distance = abs(entrada - sl)`.
  * **Dimensionamiento sobre Distancia Real:** `_lote_por_riesgo` dimensiona el volumen de lotes exactamente sobre esa distancia real:
  $$\text{Lote} = \frac{\text{Capital Base} \times \text{Riesgo \%}}{\text{Distancia Real} \times \text{Tick Value}}$$
  * Ya sea que el stop mida 12 pips o 22 pips por volatilidad, **el riesgo en dólares comprometido es idéntico** ($125 en cuenta de $25k).
* En `api/services/autotrader.py` e `index.html`:
  * El AutoTrader y el modal de confirmación envían los niveles absolutos calculados por el plan institucional, garantizando coherencia absoluta entre gráfico, plan y orden ejecutada.

---

## 3. Pruebas y Verificación

* `tests/unit/test_indicators.py`: pruebas exhaustivas de cálculo de ATR con gaps, SL estructural BUY/SELL, colchón antiruido, piso `sl_min` e imán de liquidez.
* `tests/unit/test_execution_service.py`: pruebas de ejecución a mercado con nivel absoluto `sl`, rechazo de stops invertidos y dimensionamiento exacto del lote.
* `tests/unit/test_setup_gate.py`: validación de puertas y alineación del umbral `0.5x SL`.
* **Resultado:** **769 tests pasando en verde** en la suite completa.
