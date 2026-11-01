# M22 — Control de Exposición por Clusters Macro, Límite de Posiciones y Sizing Dinámico por Buffer

**Fase:** P1 — Gestión de Riesgo y Protección de Capital  
**Fecha:** 2026-11-01  
**Decisión de Arquitectura:** `D-039`  
**Estado:** IMPLEMENTADO y VERIFICADO (100% test coverage)

---

## 1. Motivación y Diagnóstico (Audit #8 y #9)

### 1.1 El problema de la correlación macro oculta
El sistema opera sobre una lista de 12 símbolos permitidos:
- **Bloque AUD:** `AUDCAD`, `AUDCHF`, `AUDJPY`, `AUDNZD`, `AUDUSD` (5 pares con la misma divisa base).
- **Bloque USD:** `EURUSD`, `GBPUSD`, `USDCAD`, `USDCHF`, `USDJPY`, `AUDUSD`, `XAUUSD`.
- **Bloque JPY:** `USDJPY`, `EURJPY`, `AUDJPY`.
- **Metal / Oro:** `XAUUSD`.

Anteriormente no existía control sobre posiciones simultáneas por símbolo ni por cluster macro. Con un límite de operaciones diario (`max_trades_day: 3`) a un riesgo de 0.5%, el autotrader o ejecuciones consecutivas podían disparar 3 operaciones simultáneas sobre cruces de AUD o USD en cuestión de segundos ante un único impulso macro. Esto representaba una apuesta concentrada de 1.5% al mismo factor de riesgo, desvirtuando la diversificación. Además, dos eventos consecutivos sobre el mismo par abrían dos posiciones duplicadas.

### 1.2 El problema del dimensionamiento insensible al drawdown
El cálculo del tamaño de lote siempre dimensionaba sobre el capital inicial fijo (`initial_capital = $25,000` $\to$ 0.5% = $125). Si la cuenta acumulaba una mala racha y se encontraba con el buffer interno de FTMO casi agotado (por ejemplo, restando solo $100 de margen antes del corte de liquidación), el sistema seguía arriesgando $125, quebrando la cuenta en una única operación.

---

## 2. Solución Arquitectónica

### 2.1 Módulo Puro `core/clusters.py`
Se implementó un módulo determinista sin dependencias externas:
- **`DEFAULT_CLUSTERS`:** Mapeo de clusters canónicos (`AUD`, `USD`, `JPY`, `XAU`, `EUR`, `GBP`, `CAD`, `CHF`, `NZD`) para los 12 símbolos y descomposición automática de pares de 6 caracteres.
- **`calculate_open_risk_r(pos)`:** Cálculo del riesgo abierto vivo en múltiplos de R:
  - Si una posición viva ya tiene su Stop Loss en o por encima de Break-Even (`sl >= price_open` en BUY, o `sl <= price_open` en SELL), su riesgo sobre el capital se reduce a **0.0R**, liberando inmediatamente el cupo para tomar nuevas oportunidades.
  - Si no ha llegado a Break-Even, contabiliza su riesgo planificado (1.0R o `risk_r` explícito).
- **`check_exposure_limits(open_positions, new_symbol, new_risk_r, ...)`:**
  1. **Límite por Símbolo (`max_positions_per_symbol: 1`):** Rechaza con `403 MAX_POSITIONS_PER_SYMBOL_REACHED` si ya hay una orden viva en el mismo par.
  2. **Límite por Cluster (`max_positions_per_cluster: 1`):** Rechaza con `403 CLUSTER_EXPOSURE_REACHED` si ya hay una orden abierta en cualquiera de las divisas que componen el símbolo solicitado.
  3. **Tope de Riesgo Abierto Agregado (`max_total_open_risk_r: 1.5`):** Rechaza con `403 MAX_OPEN_RISK_REACHED` si la suma de R abiertas más el nuevo setup supera el máximo configurado.

### 2.2 Sizing Dinámico por Buffer en `core/prop_rules.py`
Se implementó la función pura `dynamic_risk_pct`:
$$\text{risk\_pct\_efectivo} = \min\left(\text{base\_risk\_pct},\; \frac{\text{buffer\_restante\_usd}}{2 \times \text{capital\_inicial}} \times 100,\; \frac{\text{daily\_room\_usd}}{2 \times \text{capital\_inicial}} \times 100\right)$$

- Con buffer abundante ($1,750 restante en un reto de $25k):
  $$\frac{\$1,750}{2 \times \$25,000} \times 100 = 3.5\% \implies \min(0.5\%, 3.5\%) = 0.50\% \quad (\text{sin alteración})$$
- Con buffer bajo ($100 restante):
  $$\frac{\$100}{2 \times \$25,000} \times 100 = 0.20\% \implies \min(0.5\%, 0.20\%) = 0.20\% \quad (\text{escala lote a la mitad})$$
- Con buffer en $50:
  $$\frac{\$50}{2 \times \$25,000} \times 100 = 0.10\% \implies \min(0.5\%, 0.10\%) = 0.10\%$$
- Si el buffer es $\le 0$:
  Devuelve `0.0%`, bloqueando la orden.
Ninguna operación individual puede arriesgar más del 50% del buffer restante del challenge ni del margen diario.

### 2.3 Integración en `ExecutionService` (Gate de Capital)
- **Paso 3c:** Se insertó la verificación de exposición y clusters inmediatamente después del límite diario de operaciones (`max_trades_day`). Al ser un **gate de capital**, **no puede ser saltado mediante `force=True`**, garantizando disciplina institucional.
- **Paso 4c:** El dimensionamiento de lote evalúa `dynamic_risk_pct`. Si el riesgo fue reducido por buffer, se añade una anotación explícita en los avisos de la orden.
- **Consistencia de Salidas:** Se alinearon los defaults en `core/strategy.py` (`partial_ratio_r: 1.0`, `runner_ratio_r: 2.0`) con `config/strategy.yaml`.

---

## 3. Verificación y Resultados de Tests

1. **Pureza de Núcleo (`test_core_purity.py`):** 78 tests verificando que `core/` no tiene dependencias impuras (cero I/O, cero MT5).
2. **Clusters y Buffer (`test_clusters.py`):** 13 tests unitarios verificando:
   - Detección de clusters canónicos y fallback.
   - Liberación de riesgo en Break-Even.
   - Rechazo de duplicados por símbolo.
   - Rechazo de duplicados por cluster (AUD, USD).
   - Rechazo de exceso de riesgo agregado en R.
   - Sizing dinámico ante buffer total y diario bajo.
3. **Servicio de Ejecución (`test_execution_service.py`):** 117 tests verificando:
   - Protección infranqueable con `force=True`.
   - Reducción real de lotes en órdenes enviadas al broker.
