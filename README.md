# 🦅 FTMO Copilot & Autonomous Trading Engine (v2.0)

<div align="center">

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python Version](https://img.shields.io/badge/python-3.12%2B-brightgreen.svg)
![MetaTrader 5](https://img.shields.io/badge/MetaTrader-5-orange.svg)
![Tests](https://img.shields.io/badge/tests-761%20passed-success.svg)
![Architecture](https://img.shields.io/badge/architecture-Pure%20Core%20%2B%20FastAPI-informational.svg)

**Plataforma algorítmica cuantitativa y copiloto determinista para challenges de prop firms (FTMO $25K 1-Step & 2-Step) sobre MetaTrader 5.**

[Características](#-características-principales) •
[Arquitectura](#-arquitectura-del-sistema) •
[Estrategia Cuantitativa](#-estrategia-smc--institucional) •
[Instalación](#-instalación-y-despliegue) •
[Documentación](#-documentación-adicional)

</div>

---

## 📌 Visión General

**FTMO Copilot v2.0** es un ecosistema institucional de análisis y ejecución que combina **Smart Money Concepts (SMC)** con análisis multi-temporal de alta temporalidad (**HTF: MN1, W1, D1, H4**), flujo de contratos de **Futuros CME**, sentimiento macro mediante un **Tribunal Tripartito IA** y un **Worker Autónomo** con gestión parcial y protección matemática de capital.

Opera en modo **Copiloto Asistido** (análisis, confirmación humana y métricas en vivo) o en modo **Bot Autónomo Desatendido** con auto-gestión de parciales y Stop Loss a Break-Even.

---

## ✨ Características Principales

### 🎯 1. Motor SMC Puro y Determinista (`M15`)
* **Fair Value Gaps (FVG):** Detección en tiempo real de ineficiencias de precio vivas (mitigación estricta por mecha).
* **Order Blocks (OB):** Identificación precisa de bloques institucionales tras ruptura de estructura (*BOS*).
* **Liquidity Sweeps:** Barridos de liquidez del día anterior (`PDH` / `PDL`) validados exclusivamente dentro de la sesión actual.
* **Invalidación Distal:** Stop Loss anclado al borde distal de la zona SMC (anula el setup si se rompe la tesis estructural).

### 🌐 2. Matriz de Alta Temporalidad (HTF: MN1, W1, D1, H4)
* **Dealing Range:** Determina la ubicación matemática del precio en zonas de **Premium** (ventas) o **Discount** (compras).
* **Confluencia Estructural:** Puntuación dinámica (`-10` a `+15 pts`) por alineación de tendencia en W1, D1 y H4.
* **Niveles Semanales y Mensuales:** Trazado de `PWH`/`PWL` (semana previa) y `PMH`/`PML` (mes previo).
* **Imán de Liquidez:** Identificación automática del objetivo institucional mayor más cercano.

### 🐋 3. Huella Institucional CME & Divergencias SMT
* **Contratos de Futuros Regulados:** Conexión directa con CME/COMEX para `6E1!` (Euro), `GC1!` (Oro), `6B1!` (Libra), `6A1!` (Aussie), etc.
* **Detección de Absorción:** Alertas de rechazo de mecha con volumen anómalo ($\ge 1.4\times$).
* **Divergencias SMT:** Detección de grietas estructurales contra el Índice Dólar (`DXY`) con soporte de correlación inversa y directa.

### ⚖️ 4. Tribunal Tripartito EUR/USD (Grounding Real)
* **Debate Adversarial:** Contrapone la tesis del *Fiscal Técnico* (MT5 + SMC) contra el *Abogado del Diablo* (Riesgos macro y trampas de liquidez).
* **Dictamen del Juez:** Emite veredicto ponderado con búsqueda web en vivo (*Google Search Grounding* + RSS BCE/Fed):
  * `ALTA PROBABILIDAD`: Riesgo normal 0.50%.
  * `RIESGO REDUCIDO`: Riesgo a la mitad 0.25%.
  * `VETO`: Bloqueo total ante noticias de altísimo impacto.

### 🤖 5. Bot Autónomo (`AutoTrader Daemon`) & Salida Escalonada
* **Bucle en Segundo Plano:** Escanea los 12 pares cada 30s y ejecuta en MT5 si el score cumple todos los filtros ($\ge 59.5$).
* **Salida Escalonada:**
  * **A 1:1R:** Cierra automáticamente el **50% de la posición** y mueve el SL a **Break-Even**.
  * **Hacia 1:2R (o más):** El 50% restante corre con **Riesgo Cero**.
* **Protección FTMO:** Hard gates de 3 operaciones diarias, límite de pérdida diaria y trailing drawdown.
* **Interruptor UI:** Toggle interactivo `[ 🤖 AUTÓNOMO: ON / OFF ]` con protección de token.

### 📈 6. Reconciliación de Resultados & Auditoría Cuantitativa (M20 - P0)
* **Reconciliación Automática MT5:** Sincroniza cada deal de salida (`history_deals_get`) con su orden de entrada y `setup_log` de forma idempotente en `trade_marks`.
* **Cómputo Financiero Riguroso:** PnL neto real descontando comisiones y swaps completos de entrada y salida.
* **R-Múltiplo Realizado:** Mide el rendimiento exacto sobre la distancia real del Stop Loss planificado:
  $$R = \frac{\Delta P}{|P_{open} - SL|}$$
* **Clasificación Post-Mortem:** Separa salidas por `TP`, `SL`, `BE` (Break-Even), `TP_PARTIAL` y manuales.
* **Excursiones MAE & MFE:** Registra la máxima excursión adversa y favorable en R para auditar si los stops son barridos por ruido.
* **Métricas Institucionales:** Endpoint `/api/audit/performance` con Expectancy en R, Profit Factor, desglose por buckets de score (`<60`, `60-70`, `70-80`, `80+`) y rentabilidad por par.

---

## 🏛 Arquitectura del Sistema

El proyecto sigue una estricta separación de responsabilidades:

```
ftmo_dashboard/
├── core/                # Lógica PURA (sin I/O, sin MT5, 100% testeable)
│   ├── smc_engine.py          # FVG, OB, Sweeps, Matriz HTF y Confluencia
│   ├── levels.py              # Swings, PDH/PDL, PWH/PWL, PMH/PML, Redondos
│   ├── setup_gate.py          # Puerta de decisión, desempate y R:R
│   ├── risk_engine.py         # Setup score (SMC 85% + Killzone 15%)
│   ├── exit_policy.py         # Gestión parcial escalada y Break-Even
│   ├── prop_rules.py          # Reglas del challenge FTMO (1-Step / 2-Step)
│   └── institutional_volume.py# Futuros CME, absorción y divergencias SMT
├── adapters/            # Conexión externa aislada
│   ├── mt5/                   # Terminal MT5 (Forex spot, lectura y órdenes)
│   └── tradingview/           # Futuros CME en tiempo real vía WebSocket
├── api/                 # Backend FastAPI
│   ├── routes/                # Endpoints delgados (market, trade, autotrader, audit)
│   └── services/              # Orquestadores (ReconciliationService, AutoTraderService, MarketService)
├── database/            # Capa de persistencia SQLite (trading.db)
├── static/              # Dashboard interactivo (TradingView Lightweight Charts v4)
└── tests/               # 761 tests automatizados (unitarios y de integración)
```

---

## 📊 Matriz de Símbolos Operables

El copiloto analiza y opera los 12 pares principales de FTMO en **M15**:

| Par / Activo | Tipo | Futuro CME | Tick Pip | SL Base |
| :--- | :--- | :--- | :--- | :--- |
| **EURUSD** | Forex Mayor | `CME:6E1!` | 0.0001 | 12.0 pips |
| **GBPUSD** | Forex Mayor | `CME:6B1!` | 0.0001 | 15.0 pips |
| **USDCAD** | Forex Mayor | `CME:6C1!` | 0.0001 | 12.0 pips |
| **USDJPY** | Forex Mayor | `CME:6J1!` | 0.0100 | 12.0 pips |
| **USDCHF** | Forex Mayor | `CME:6S1!` | 0.0001 | 12.0 pips |
| **AUDUSD** | Forex Mayor | `CME:6A1!` | 0.0001 | 12.0 pips |
| **XAUUSD** | Metal (Oro) | `COMEX:GC1!`| 0.0100 | 8.0 puntos |
| **EURJPY** | Cruce Forex | `CME:6E1!` / `6J1!` | 0.0100 | 15.0 pips |
| **AUDCAD** | Cruce Forex | `CME:6A1!` / `6C1!` | 0.0001 | 12.0 pips |
| **AUDNZD** | Cruce Forex | `CME:6A1!` | 0.0001 | 15.0 pips |
| **AUDCHF** | Cruce Forex | `CME:6A1!` | 0.0001 | 12.0 pips |
| **AUDJPY** | Cruce Forex | `CME:6A1!` / `6J1!` | 0.0100 | 15.0 pips |

---

## 🚀 Instalación y Despliegue

### Requisitos
* Windows 10/11 (MetaTrader 5 Python API requiere entorno nativo Windows).
* Python 3.12 o superior.
* Terminal MetaTrader 5 instalada con cuenta Demo o Real.

### Paso a Paso

1. **Clonar el repositorio:**
   ```powershell
   git clone https://github.com/mauroferrera/ftmoV2.git
   cd ftmoV2/ftmo_dashboard
   ```

2. **Instalar dependencias:**
   ```powershell
   pip install -r requirements.txt
   ```

3. **Configurar variables de entorno:**
   Copia la plantilla y edita tus credenciales:
   ```powershell
   Copy-Item .env.example .env
   ```
   Configura en `.env`:
   ```env
   MT5_LOGIN=tu_numero_de_cuenta
   MT5_PASSWORD=tu_contraseña
   MT5_SERVER=tu_servidor_broker
   MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
   API_TOKEN=tu_token_seguro
   FTMO_PROFILE=ftmo_2step_25k
   ```

4. **Verificar la suite de pruebas:**
   ```powershell
   python -m pytest
   # 761 passed en verde
   ```

5. **Iniciar el Dashboard:**
   ```powershell
   python app.py
   ```
   Abre **http://127.0.0.1:8000** en tu navegador.

---

## 📈 Análisis de Rendimiento y Post-Mortem

El sistema audita de forma persistente cada decisión en `database/trading.db`. Puedes consultar las métricas de rendimiento y forzar reconciliaciones con:

```http
GET /api/audit/performance
POST /api/audit/reconcile?days=30
GET /api/audit/marks?limit=100
```

Respuesta estructurada con:
* Tasa de acierto global (*Winrate %*), *Expectancy en R* y *Profit Factor*.
* Desglose por buckets de score (`<60`, `60-70`, `70-80`, `80+`).
* Desglose por símbolo y por motivo de salida (`TP`, `SL`, `BE`, `PARTIAL`, manual).
* Historial de trades cerrados emparejados con su respectivo ticket de MT5.

Para más detalles técnicos, consulta el documento [M20 — Reconciliación de Deals MT5 y Auditoría Post-Mortem](docs/M20_RECONCILIACION_AUDITORIA.md).

---

## 🛡 Descargo de Responsabilidad (Disclaimer)

*Este software es una herramienta avanzada de investigación y asistencia cuantitativa al trading. El trading de divisas y contratos apalancados implica un riesgo sustancial de pérdida. Las pruebas pasadas y los entornos de simulación no garantizan resultados futuros. Utiliza siempre cuentas demo antes de considerar la operativa en cuentas reales.*
