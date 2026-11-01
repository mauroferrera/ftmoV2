# M24 — Estructura y Sesgo HTF Institucional

**Fase:** P1 — Confluencia Multi-Temporal y Estructura Mayor  
**Fecha:** 2026-11-01  
**Decisión de Arquitectura:** `D-041`  
**Estado:** IMPLEMENTADO y VERIFICADO (100% test coverage)

---

## 1. Motivación y Diagnóstico (Audit #5)

### 1.1 El sesgo HTF como comparación de una sola vela
Anteriormente, la función `_analyze_single_htf` en `core/smc_engine.py` determinaba el sesgo estructural (`bias`) comparando el cierre de la última vela con el máximo/mínimo de la vela previa y llamaba "BOS" a cualquier cierre ligeramente por encima o por debajo de la vela inmediata anterior. 
Asimismo, el Dealing Range para clasificar la zona de `Premium` vs `Discount` se calculaba sobre una ventana fija y arbitraria de 3 velas (`ventana = candles[-3:]`).

**Consecuencias de la heurística anterior:**
- El sesgo cambiaba de signo con cada vela intradía oscilante, convirtiendo la matriz HTF en ruido estocástico.
- El cálculo de Premium/Discount ignoraba el tramo real de mercado (Dealing Range leg) entre los verdaderos puntos de inflexión institucional.
- Dado que el bonus/penalización HTF ($\pm 15$ pts) y la tarjeta "Matriz HTF" cuelgan de este análisis, era la pieza con mayor ratio de retorno analítico pendiente.

---

## 2. Solución Arquitectónica

### 2.1 Swings Confirmados y Ruptura Real de Estructura (BOS / CHoCH)
En `core/smc_engine.py`:
- Se implementó la detección de pivotes confirmados en altas temporalidades (`MN1`, `W1`, `D1`, `H4`) mediante `swing_pivots`:
  - `swings_h`: secuencia de Swing Highs institucionales confirmados.
  - `swings_l`: secuencia de Swing Lows institucionales confirmados.
- **Ruptura de Estructura (BOS):**
  - Un cierre de vela por encima del último Swing High confirmado genera un **BOS Alcista**.
  - Un cierre de vela por debajo del último Swing Low confirmado genera un **BOS Bajista**.
- **Cambio de Carácter (CHoCH):**
  - Si la secuencia previa de swings era bajista (Lower Highs + Lower Lows) o la tendencia EMA venía descendente y el precio rompe el último Swing High, se clasifica como **CHoCH Alcista** (reversión estructural mayor).
  - A la inversa, si la secuencia era alcista y se quiebra el último Swing Low, se clasifica como **CHoCH Bajista**.

### 2.2 Dealing Range Institucional (Swing-to-Swing)
El Dealing Range ya no se extrae de 3 velas:
- Se ancla estrictamente a los extremos institucionales del tramo activo:
  - $\text{range\_high} = \max(\text{Swing High}_{\text{activo}},\; \text{High}_{\text{última vela}})$
  - $\text{range\_low} = \min(\text{Swing Low}_{\text{activo}},\; \text{Low}_{\text{última vela}})$
- Se calcula la posición relativa en el rango:
  $$\text{range\_pct} = \frac{\text{precio} - \text{range\_low}}{\text{range\_high} - \text{range\_low}} \times 100$$
- **Zonas Institucionales:**
  - `PREMIUM`: $\text{range\_pct} \ge 55\%$ (zona óptima de venta mayor).
  - `DISCOUNT`: $\text{range\_pct} \le 45\%$ (zona óptima de compra mayor).
  - `EQUILIBRIUM`: $45\% < \text{range\_pct} < 55\%$.

### 2.3 Contexto de Tendencia y Momentum (EMA pura)
Se añadió la función pura `_calculate_ema(values, period=9)`:
- Evalúa el precio respecto a la EMA(9) del timeframe mayor y la pendiente de los últimos 3 periodos.
- Cuando el precio está dentro del rango sin una ruptura fresca en la última vela, el sesgo respeta la tendencia mayor y la secuencia de Higher Highs / Lower Lows, evitando falsos giros de sesgo por velas menores de retroceso.

---

## 3. Verificación
- `tests/unit/test_core_purity.py`: 78 tests verificando pureza estricta (cero pandas, cero numpy).
- `tests/unit/test_smc_engine.py`: 17 tests unitarios cubriendo detección de BOS sobre Swing High, detección de CHoCH, Dealing Range swing-to-swing y clasificación Premium/Discount.
