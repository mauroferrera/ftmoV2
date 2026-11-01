# M23 — Score Continuo, Ponderación Real de Killzone y Calidad de Zonas SMC

**Fase:** P1 — Señales y Evaluación de Setups  
**Fecha:** 2026-11-01  
**Decisión de Arquitectura:** `D-040`  
**Estado:** IMPLEMENTADO y VERIFICADO (100% test coverage)

---

## 1. Motivación y Diagnóstico (Audit #3, #4, #6)

### 1.1 Killzone decorativa y score discreto (#3)
Con la calibración previa (`weights: smc: 85, killzone: 15` y `min_score: 59.5`):
- $85 \times 0.70 = 59.5$: el valor SMC base de $0.70$ (un simple FVG con OB o Sweep) alcanzaba exactamente el umbral de aprobación con $0$ puntos de sesión.
- Dentro de la Killzone: $85 \times 0.7 + 15 \times 1.0 = 74.5$ (Aprobado).
- Fuera de la Killzone: $85 \times 0.7 + 15 \times 0.25 = 63.25$ (Aprobado).
La sesión de mercado solo alteraba el veredicto en un único escalón atípico ($0.60$). Para el resto, la estrategia se reducía a un booleano estático "¿hay FVG y (OB o sweep)?", dejando que el sistema operara en horarios de bajo volumen o fines de semana.

### 1.2 Confluencia HTF falseada y desacoplada del Gate (#4)
La función `evaluate_htf_confluence` ($\pm 15$ pts) calculaba el bonus en `setup_eval` únicamente para la interfaz gráfica. El gate (`setup_gate`), el watcher y el autotrader evaluaban solo el `base_score`. Además, la UI sobrescribía el score visible con `score_with_htf`, permitiendo anomalías visuales donde un setup con score 70 se mostraba rechazado, o uno con score 52 aparecía aprobado.

### 1.3 Zonas SMC sin edad ni calidad (#6)
`detect_fvgs` y `detect_order_blocks` devolvían todas las zonas no mitigadas del historial de 300 velas con el mismo peso; un FVG de hace 3 días tenía idéntica validez que uno formado hace 20 minutos. No existía noción de frescura, proximidad en ATR ni volumen de desplazamiento institucional.

---

## 2. Solución Arquitectónica

### 2.1 Calidad y Edad de Zonas en `core/smc_engine.py`
- **Filtro de Edad Máxima (`MAX_ZONE_AGE_CANDLES = 120`):** En timeframe M15, 120 velas equivalen a 30 horas de operativa de mercado. Las zonas más antiguas se descartan de la operativa viva por haber perdido relevancia institucional.
- **Métricas Granulares por Zona:**
  - `age_candles`: antigüedad exacta de la zona en velas.
  - `freshness`: función continua de $0.5$ a $1.0$ según edad: $\max(0.5, 1.0 - 0.5 \times \frac{\text{age\_candles}}{\text{max\_age}})$.
  - `volume_ratio`: ratio de volumen de la vela impulsiva respecto a la media móvil de 20 periodos (`vol / mean_vol_20`). Si es $\ge 1.3$, confirma desplazamiento institucional genuino.

### 2.2 Score Continuo en `core/risk_engine.py`
La función `smc_component` evoluciona de escalones discretos a una evaluación continua y calibrable:
- **Base estructural:** $1.0$ (FVG + OB + Sweep), $0.70$ (FVG + OB/Sweep), $0.50$ (un solo factor).
- **Ajuste por frescura:** $\pm 0.05$ según el promedio de frescura de las zonas a favor.
- **Ajuste por volumen:** $+0.05$ si la vela de origen muestra confirmación institucional ($\text{volume\_ratio} \ge 1.3$).
- **Ajuste por proximidad en ATR:** Si la zona más cercana está a más de $2.0 \cdot \text{ATR}_{14}$ del precio actual, penaliza $-0.05$.
- **Penalización por contrapartida:** $-0.05$ por cada zona contraria activa viva interpuesta (máx $-0.10$).

### 2.3 Ponderación Real de la Killzone
- Se fijaron los pesos en `weights: smc: 80.0, killzone: 20.0` y `min_score: 59.5`.
- `killzone_score` devuelve `value: 0.0` fuera de sesión (Londres 07:00-10:00 UTC / NY 12:30-15:30 UTC):
  - Setup estándar ($0.70$ SMC) **dentro de Killzone:** $80 \times 0.70 + 20 \times 1.0 = 76.0 \ge 59.5 \implies$ **APROBADO**.
  - Mismo setup **fuera de Killzone:** $80 \times 0.70 + 20 \times 0.0 = 56.0 < 59.5 \implies$ **RECHAZADO**.
La killzone ahora **decide activamente**, filtrando el ruido de sesiones muertas.

### 2.4 Unificación de Confluencia HTF en `core/setup_gate.py`
- El gate de entrada evalúa la confluencia multi-temporal (`evaluate_htf_confluence`) directamente:
  $$\text{score\_efectivo} = \min(100.0, \max(0.0, \text{base\_score} + \text{htf\_bonus}))$$
- Tanto el gate de aprobación (`approved = score_effective >= umbral`), como el autotrader, el watcher y la UI consumen el mismo `score_effective`.
- Si el score es insuficiente, el motivo explica el desglose exacto:
  `"Score 54.0 (base 64.0 + HTF -10.0) < umbral 60."`

---

## 3. Verificación
- `tests/unit/test_core_purity.py`: 78 tests verificando pureza estricta.
- `tests/unit/test_smc_engine.py`: 15 tests verificando detección de zonas vivas, descarte de zonas viejas $> 120$ velas y metadatos de calidad.
- `tests/unit/test_setup_gate.py`: 22 tests verificando que la Killzone aprueba dentro de sesión y rechaza fuera, y que el bonus HTF aprueba/rechaza sincronizado con el veredicto del gate.

