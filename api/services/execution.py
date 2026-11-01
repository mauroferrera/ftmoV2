"""Ejecución: decide si esta orden puede salir, y si puede, la manda.

Este módulo es la CONSECUENCIA de mandar dinero, así que es el sitio donde se
exige que cada puerta se pueda leer. La regla del fichero es una sola:

    **Una orden no se manda sin que las cosas que la justifican estén comprobadas
    en el MISMO instante: que el símbolo está permitido, que el estado FTMO del día
    lo admite, y que la orden que se va a enviar es la que el gate aprobó.**

Lo último es el punto donde fallan los sistemas. Es habitual calcular bien el
lote, pasar el riesgo, y enviar con un precio o un stop que ya no son los
validados; el resultado es una orden viva con una tesis que nadie ha mirado. Por
eso aquí los niveles se planifican DESPUÉS de leer la cotización, se validan, y se
envían tal cual: el `price` que sale de `order_send` es el que se ha medido.

**El orden de las puertas no es arbitrario** y cambiarlo cambia la seguridad:

1. Símbolo y acción: una petición mal formada no debe tocar el bróker.
2. `symbols_allow`: la lista blanca del usuario, antes de leer su cuenta.
3. FTMO: los topes (diario y total) son del DÍA, y este es el primer momento en que
   se sabe que son los de hoy. La cuenta se evalúa contra su perfil de
   `config/prop_profiles.yaml` (fail-closed: sin perfil no se opera).
4. Niveles, lote, `validate_entry` y `execution_quality`: la geometría de la orden.
5. `log_setup` y SOLO ENTONCES `order_send`.

**Por qué `log_setup` va antes del envío.** Una orden que sale y no se puede
explicar es indistinguible de una orden que nunca se intentó. Si el proceso
muere entre el envío y el registro, la fila sigue ahí con `validated=1` y el
resultado vacío, que es el estado que dice "se intentó y no consta". Al revés
—registrar después— deja operaciones sin fila, y una operación sin fila no
sabe ni si ocurrió.

**Recortes de esta fase frente a Trinity.**
- **El gate de noticias NO existe** (D-012): se fue con el calendario, así que
  tampoco hay `VERDICT_IGNORED_NEWS` ni fail-open que declarar.
- **El gate FTMO no es un `daily_risk_state` leído del mercado**, sino el veredicto
  del `PropService` (`core/prop_rules.evaluate`): el estado prop-firm vive en su
  módulo y aquí solo se obedece.
- **El dimensionado usa la API de `core/lot_calculator` de este proyecto**
  (`standard_forex_lots(balance, risk_pct, sl_distance, spec)`), que no es la de
  Trinity: se ancla en el capital FIJO del perfil, no en el balance vivo.
- **La lectura (`preview_entry`, `trade_info`) no entra aquí**: este servicio es la
  puerta de ESCRITURA (abrir, cerrar, modificar).
"""

from __future__ import annotations

from contextlib import nullcontext
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from core import exit_policy, execution_quality, lot_calculator, risk_engine, strategy
from core.clock import now_iso

#: Timeframe con el que se registra el setup cuando quien llama no lo dice.
DEFAULT_TIMEFRAME = "M15"


def _con_cuenta(fn):
    """Mantiene la sesión en la cuenta del servicio durante toda la operación.

    Con varias cuentas la terminal cambia de login con `mt5.login()`. Si una
    petición de la cuenta A se intercalara con otra de la cuenta B entre dos
    llamadas (leer el precio y mandar la orden), la orden podría salir en la
    cuenta equivocada. El cerrojo de la sesión serializa la operación completa.
    En los tests con dobles no hay sesión y el cerrojo es un no-op.
    """
    @wraps(fn)
    def _wrapper(self, *args, **kwargs):
        with self._cuenta_guard():
            return fn(self, *args, **kwargs)
    return _wrapper


def _pip_de(spec: Any) -> Optional[float]:
    """El pip del spec si lo publica; `None` si no. No se inventa."""
    valor = getattr(spec, "pip", None)
    try:
        return float(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


class ExecutionService:
    """Puerta única de salida de órdenes. Requiere token en la capa HTTP.

    No es idempotente ni pretende serlo: mandar la misma orden dos veces abre dos
    posiciones. La protección es de quien llama (el token, y `auto_execute` apagado
    en el watcher), no una deduplicación inventada aquí que daría una falsa
    sensación de seguridad.
    """

    def __init__(self, market: Any, store: Any = None, execution: Any = None,
                 prop: Any = None, account_id: Optional[str] = None) -> None:
        self._market = market
        self._store = store
        self._execution = execution
        self._prop = prop
        # Con varias cuentas, el puerto de ejecución se pide por cuenta desde el
        # runtime (`_build_execution(account_id)`); este servicio solo lo registra
        # para saber de qué cuenta es la puerta que usa.
        self._account_id = account_id

    def _cuenta_guard(self):
        """Cerrojo de cuenta de la sesión real, o no-op con dobles de test."""
        sesion = getattr(self._execution, "session", None)
        if sesion is None:
            sesion = getattr(self._market, "session", None)
        if sesion is not None and self._account_id and hasattr(sesion, "account"):
            return sesion.account(self._account_id)
        return nullcontext()

    # -- configuración ----------------------------------------------------------

    def _cfg(self) -> Dict[str, Any]:
        """Config de trading PLANA, nunca lanza.

        El store devuelve el `strategy.yaml` CRUDO (anidado). Se aplana con
        `core.strategy.build_flat` para que los consumidores que leen claves planas
        (`effective_risk_pct`, `validate_entry`, `execution_quality`) vean los
        valores del YAML y no sus defaults. Si el YAML viniera ya plano (un test),
        se respetan igualmente sus claves de nivel superior.

        Sin store (o con un YAML ilegible) se cae a `strategy.DEFAULTS`: son los
        mismos umbrales que trae el YAML de fábrica, así que un fallo de lectura no
        deja la ejecución a ciegas, pero `symbols_allow` vacío hace que `_permite`
        avise de que no hay lista blanca.
        """
        raw: Any = {}
        if self._store is not None:
            try:
                raw = self._store.get_trading_config() or {}
            except Exception:  # noqa: BLE001 - sin config se opera con los defaults
                raw = {}
        try:
            plano = strategy.build_flat(raw)
        except Exception:  # noqa: BLE001 - un YAML roto no debe reventar aquí
            plano = dict(strategy.DEFAULTS)
        if isinstance(raw, dict):
            for clave in strategy.DEFAULTS:
                if clave in raw:
                    plano[clave] = raw[clave]
        return plano

    @staticmethod
    def _permitidos(cfg: Dict[str, Any]) -> List[str]:
        """Símbolos permitidos, en mayúsculas, de la lista del usuario."""
        raw = strategy._primer_valor(cfg, ("symbols_allow",), ("risk", "symbols_allow"))
        if raw is None:
            return []
        if isinstance(raw, str):
            return [p.strip().upper() for p in raw.replace(",", " ").split() if p.strip()]
        if isinstance(raw, (list, tuple, set)):
            return [str(s).strip().upper() for s in raw if str(s).strip()]
        return []

    @staticmethod
    def _permite(cfg: Dict[str, Any], simbolo: str) -> Tuple[bool, Optional[str]]:
        """`(permitido, motivo)`.

        Una lista blanca VACÍA no bloquea: en un despliegue que no la ha
        configurado, bloquear todo dejaría el sistema sin poder hacer nada y
        nadie podría ni probarlo. El coste de esa elección —que una lista vacía
        no protege de nada— se declara en `motivo`, y la ruta lo publica.
        """
        permitidos = ExecutionService._permitidos(cfg)
        if not permitidos:
            return True, ("symbols_allow está vacío: se permiten todos los "
                          "símbolos. Configura la lista blanca para poder "
                          "operar de verdad.")
        if simbolo in permitidos:
            return True, None
        return False, ("{0} no está en symbols_allow ({1}).".format(
            simbolo, ", ".join(permitidos)))

    # -- auditoría --------------------------------------------------------------

    def _registra(self, entry: Dict[str, Any]) -> Optional[int]:
        """`store.log_setup` sin dejar que un fallo de auditoría tumbe la orden.

        Devolver `None` es un resultado honesto: la operación puede continuar
        (la auditoría no es un requisito de ejecución) pero la respuesta lo dice
        en `audit_logged=False`, porque una fila que no se escribió es
        información que el operador necesita aunque no le bloquee.
        """
        if self._store is None:
            return None
        try:
            return self._store.log_setup(entry)
        except Exception:  # noqa: BLE001 - la auditoría nunca tumba la ejecución
            return None

    def _graba_intentos(self, setup_id: Optional[int], resultado: Dict[str, Any]) -> None:
        """Los intentos de llenado junto a la decisión que los provocó.

        Importa el orden de los `type_filling`: si se probó FOK, falló, y se
        acabó mandando con IOC, la fila tiene que contar eso. Con un solo
        `retcode` final, el 10030 del FOK desaparecería y el rechazo parecería
        de un modo de llenado cualquiera.
        """
        if not setup_id or self._store is None:
            return
        intentos = resultado.get("attempts") or []
        if not intentos:
            return
        try:
            self._store.update_trade_result(setup_id, {
                "attempts": intentos,
                "filling": resultado.get("filling"),
                "retcode": resultado.get("retcode"),
                "deal": resultado.get("deal"),
                "order": resultado.get("order"),
                "fill_price": resultado.get("fill_price") or resultado.get("price"),
                "status": resultado.get("status"),
            })
        except Exception:  # noqa: BLE001 - la auditoría nunca tumba la ejecución
            return

    # -- FTMO -------------------------------------------------------------------

    def _estado_ftmo(self) -> Tuple[Dict[str, Any], Optional[str], int, Optional[str]]:
        """`(estado, error, status_http, etiqueta)` del gate prop-firm.

        Convierte el veredicto del `PropService` en la decisión binaria de esta
        puerta y, si bloquea, en el motivo y el status que la ruta devuelve.

        Fail-closed en los TRES casos en que no se puede operar:

        - **Sin `PropService` cableado** → 503: no es que la cuenta esté bien, es
          que no hay con qué comprobarla. Un servicio de ejecución montado sin
          prop podría mandar una orden sin saber si la cuenta ya reventó el día.
        - **`tracked=False`** → 403: la cuenta no tiene perfil en
          `config/prop_profiles.yaml`. Sin capital inicial no hay buffer y no se
          dimensiona con las reglas que se dice seguir. No se adivina el perfil.
        - **`evaluation.blocked`** → 403: el shell ya alcanzó un corte INTERNO.

        El error de lectura (bróker caído, YAML ilegible) también es 503: no
        poder leer el estado no es un aprobado silencioso.
        """
        if self._prop is None:
            return ({}, "No hay servicio de prop cableado: no se puede comprobar "
                        "el estado FTMO de la cuenta (fail-closed).", 503,
                    "FTMO_STATE_UNKNOWN")
        try:
            estado = self._prop.state() or {}
        except Exception as exc:  # noqa: BLE001 - no saber el estado bloquea
            return ({}, "No se pudo leer el estado FTMO de la cuenta: {0}".format(exc),
                    503, "FTMO_STATE_UNKNOWN")
        if not estado.get("tracked"):
            motivo = estado.get("reason") or (
                "La cuenta no sigue ningún perfil FTMO (fail-closed): no se opera.")
            return estado, motivo, 403, "BLOCKED_BY_FTMO"
        evaluado = estado.get("evaluation") or {}
        if evaluado.get("blocked"):
            motivos = "; ".join(evaluado.get("reasons") or ["riesgo bloqueado"])
            return (estado, "Operación bloqueada por los topes del día: " + motivos,
                    403, "BLOCKED_BY_RISK")
        return estado, None, 200, None

    @staticmethod
    def _base_de_riesgo(estado: Dict[str, Any]) -> Optional[float]:
        """El capital FIJO del perfil para dimensionar, o `None` si no se puede leer.

        Se dimensiona sobre `initial_capital` y no sobre el balance vivo: el buffer
        de DD total es un porcentaje de ese capital, así que anclar aquí equivale a
        dimensionar sobre el buffer. Con el balance vivo, la cuenta que gana
        arriesga más y la que pierde arriesga menos — justo al revés de la regla.
        """
        perfil = (estado or {}).get("profile") or {}
        base = perfil.get("initial_capital")
        try:
            base = float(base)
        except (TypeError, ValueError):
            return None
        return base if base > 0 else None

    # -- geometría y lote -------------------------------------------------------

    @staticmethod
    def _precio_de_entrada(action: str, precio: Dict[str, Any]) -> Optional[float]:
        """El precio al que se entra: el que se PAGA.

        Una BUY se paga al ask y una SELL al bid. Entrar al bid en una compra es
        un precio que el bróker nunca ha visto, y la diferencia es exactamente el
        spread del que el gate no se enteraría.
        """
        ask = precio.get("ask")
        bid = precio.get("bid")
        bruto = ask if action == "BUY" else bid
        try:
            v = float(bruto)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    @staticmethod
    def _lot_spec(spec: Any) -> lot_calculator.LotSpec:
        """El `LotSpec` del símbolo, leído del spec del bróker.

        Vive en una función propia porque el dimensionado y la comprobación del
        presupuesto tienen que leer EXACTAMENTE los mismos campos. Si cada uno
        armara el suyo, un campo olvidado en uno de los dos cambiaría el lote o
        el riesgo sin que nada fallara: las dos cifras seguirían siendo
        redondeadas y con aspecto de razonables.
        """
        return lot_calculator.lot_spec_from_symbol(spec)

    def _lote_por_riesgo(self, base: Optional[float], risk_pct: float,
                         sl_distance: float, spec: Any,
                         simbolo: str) -> Tuple[Optional[float], Optional[str]]:
        """`(lote, error)` dimensionado por riesgo sobre el capital FIJO.

        Usa `core/lot_calculator.standard_forex_lots(base, risk_pct, distance, spec)`
        — la MISMA cuenta que el resto del sistema, para que el lote que se envía
        sea el que se cree que se envía. Si no llega ni al mínimo del bróker, el
        lote sale `clamped_to_min` y el riesgo real excedería el presupuesto: se
        rechaza en vez de mandar una exposición que nadie pidió.
        """
        if not base or base <= 0:
            return None, ("No se pudo leer el capital fijo del perfil FTMO para "
                          "dimensionar el lote.")
        try:
            salida = lot_calculator.standard_forex_lots(base, risk_pct, sl_distance, spec)
        except ValueError as exc:
            return None, ("No se pudo dimensionar el lote para {0} con el SL de {1}: "
                          "{2}".format(simbolo, sl_distance, exc))
        lote = float(salida.get("lots") or 0.0)
        if lote <= 0:
            return None, ("El lote calculado para {0} es 0: con un SL de {1} y un "
                          "riesgo del {2}% no alcanza ni el mínimo del bróker "
                          "({3}).".format(simbolo, sl_distance, risk_pct,
                                          salida.get("clamped_to_min")))
        return lote, None

    # -- ejecución --------------------------------------------------------------

    @_con_cuenta
    def execute_market_trade(self, symbol: str, action: str = "BUY",
                             volume: Optional[float] = None,
                             sl_distance: Optional[float] = None,
                             tp_distance: Optional[float] = None, no_tp: bool = False,
                             magic: Optional[int] = None, comment: Optional[str] = None,
                             deviation: Optional[int] = None,
                             score: Optional[float] = None,
                             verdict: Optional[str] = None,
                             invalidate_level: Optional[float] = None,
                             planned_entry: Optional[float] = None,
                             created_at: Optional[float] = None,
                             components: Optional[Dict[str, Any]] = None,
                             context: Optional[Dict[str, Any]] = None,
                             timeframe: str = DEFAULT_TIMEFRAME,
                             source: str = "",
                             force: bool = False,
                             sl: Optional[float] = None,
                             tp: Optional[float] = None) -> Tuple[Dict[str, Any], int]:
        """`(cuerpo, status)`. El status lo elige el servicio, no la ruta.

        Que sea aquí y no en el handler es lo que hace que el watcher y la API
        tengan EXACTAMENTE las mismas puertas. Si cada ruta validara por su cuenta,
        el auto-arranque acabaría siendo el camino con menos filtros, que es como
        se pierde dinero sin que nadie lo decida.

        `no_tp=True` manda la orden SIN objetivo (`tp=None`): es la entrada cuya
        salida la decide una política de salida y no un target congelado que la
        validación nunca evaluó. Con `no_tp` se salta solo el cálculo del TP: el R:R
        de `validate_entry` ni se plantea sin target, y el resto de puertas
        (lista blanca, FTMO, calidad) siguen iguales.

        `force=True` salta SOLO las puertas restrictivas de OPORTUNIDAD (lista
        blanca de símbolos, R:R/invalidez/TTL del setup y calidad de ejecución):
        son filtros de "qué operar", no de "cuánto arriesgar". NUNCA salta los
        topes de CAPITAL: los cortes FTMO, la obligación de SL, el dimensionado
        del lote ni la cabeza de riesgo del presupuesto. La fila de auditoría lo
        marca (`forced=1`) y guarda los motivos saltados en `force_skipped`; la
        respuesta publica `forced` para que el panel lo enseñe.
        """
        simbolo = str(symbol or "").strip().upper()
        acc = str(action or "").strip().upper()

        # -- 1. Petición bien formada ------------------------------------------
        if not simbolo:
            return {"error": "Falta el símbolo."}, 400
        if acc not in ("BUY", "SELL"):
            return {"error": "Acción inválida: solo BUY o SELL (recibido '{0}').".format(action)}, 400

        cfg = self._cfg()
        avisos: List[str] = []
        force_skipped: List[str] = []

        # -- 2. Lista blanca ----------------------------------------------------
        permitido, nota = self._permite(cfg, simbolo)
        if nota:
            avisos.append(nota)
        if not permitido:
            if force:
                force_skipped.append(
                    "Lista blanca saltada a propósito (forzado): " + nota)
            else:
                return {"error": nota, "symbol": simbolo,
                        "status": "SYMBOL_NOT_ALLOWED"}, 403

        # -- 3. FTMO: topes del día (fail-closed) ------------------------------
        estado_ftmo, error_ftmo, status_ftmo, etiqueta = self._estado_ftmo()
        if error_ftmo:
            return {"error": error_ftmo, "symbol": simbolo, "status": etiqueta}, status_ftmo

        # -- 3b. Límite diario de operaciones (capital / disciplina de sobreoperar) -
        max_trades = int(cfg.get("max_trades_day") or 0)
        if max_trades > 0:
            st = (estado_ftmo.get("evaluation") or {}).get("state", {})
            trades_hoy = int(st.get("day_trades_count", 0))
            if trades_hoy >= max_trades:
                return {
                    "error": f"Límite diario de operaciones alcanzado ({trades_hoy}/{max_trades} trades hoy).",
                    "symbol": simbolo,
                    "status": "MAX_TRADES_DAY_REACHED",
                }, 403

        # -- 4a. Spec y cotización ---------------------------------------------
        try:
            spec = self._market.spec(simbolo)
        except Exception as exc:  # noqa: BLE001 - spec ilegible = no se opera
            return {"error": "No se pudo leer la especificación de {0}: {1}".format(simbolo, exc),
                    "symbol": simbolo, "status": "SPEC_UNAVAILABLE"}, 503
        if spec is None:
            return {"error": "El bróker no publica el símbolo {0}.".format(simbolo),
                    "symbol": simbolo, "status": "SYMBOL_NOT_FOUND"}, 404

        try:
            precio = self._market.price(simbolo) or {}
        except Exception as exc:  # noqa: BLE001 - sin precio no hay orden a mercado
            return {"error": "No se pudo cotizar {0}: {1}".format(simbolo, exc),
                    "symbol": simbolo, "status": "PRICE_UNAVAILABLE"}, 503
        entrada = self._precio_de_entrada(acc, precio)
        if entrada is None:
            return {"error": "Cotización incompleta para {0}: no se manda una orden "
                             "a mercado sin precio.".format(simbolo),
                    "symbol": simbolo, "status": "PRICE_UNAVAILABLE"}, 503

        # -- 4b. Distancias y Niveles Estructurales ----------------------------
        digits = getattr(spec, "digits", 5) if spec else 5
        signo = 1.0 if acc == "BUY" else -1.0

        if sl is not None:
            sl_val = round(float(sl), digits)
            calc_dist = (entrada - sl_val) if acc == "BUY" else (sl_val - entrada)
            if calc_dist <= 0:
                return {
                    "error": f"SL para {acc} ({sl_val}) debe quedar del lado correcto de la entrada ({entrada}).",
                    "symbol": simbolo,
                    "status": "INVALID_SL_LEVEL",
                }, 400
            sl = sl_val
            sl_distance = calc_dist
            origen_sl = "structural_request"
        elif sl_distance is not None:
            sl_distance = float(sl_distance)
            if sl_distance <= 0:
                return {"error": f"Distancia de stop loss inválida: {sl_distance} <= 0.",
                        "symbol": simbolo, "status": "INVALID_SL_DISTANCE"}, 400
            sl = round(entrada - signo * sl_distance, digits)
            origen_sl = "request"
        else:
            sl_distance, origen_sl = strategy.sl_distance_for(cfg, simbolo, spec)
            if sl_distance is None:
                clave = "sl_distance_by_symbol.{0}".format(simbolo) if simbolo else "sl_distance"
                return {"error": "No hay distancia de SL configurada para '{0}' "
                                 "(strategy.yaml: sl_distance o "
                                 "sl_distance_by_symbol): no se inventa un stop.".format(clave),
                        "symbol": simbolo, "status": "NO_SL_DISTANCE"}, 400
            sl_distance = float(sl_distance)
            sl = round(entrada - signo * sl_distance, digits)

        if no_tp:
            if tp_distance is not None or tp is not None:
                avisos.append("no_tp=True: se ignora tp/tp_distance y la orden sale "
                              "sin objetivo (la salida la decide la política de "
                              "salida de la estrategia).")
            tp_distance = None
            tp = None
        elif tp is not None:
            tp_val = round(float(tp), digits)
            calc_tp_dist = (tp_val - entrada) if acc == "BUY" else (entrada - tp_val)
            if calc_tp_dist <= 0:
                return {
                    "error": f"TP para {acc} ({tp_val}) debe quedar del lado correcto de la entrada ({entrada}).",
                    "symbol": simbolo,
                    "status": "INVALID_TP_LEVEL",
                }, 400
            tp = tp_val
            tp_distance = calc_tp_dist
        else:
            if tp_distance is None:
                tp_distance = sl_distance * strategy.tp_ratio_for(cfg)
            tp_distance = float(tp_distance)
            if tp_distance <= 0:
                return {"error": f"Distancia de take profit inválida: {tp_distance} <= 0.",
                        "symbol": simbolo, "status": "INVALID_TP_DISTANCE"}, 400
            tp = round(entrada + signo * tp_distance, digits)

        if acc == "BUY":
            if sl >= entrada:
                return {"error": f"SL para BUY ({sl}) debe ser menor que la entrada ({entrada}).",
                        "symbol": simbolo, "status": "INVALID_SL_LEVEL"}, 400
            if tp is not None and tp <= entrada:
                return {"error": f"TP para BUY ({tp}) debe ser mayor que la entrada ({entrada}).",
                        "symbol": simbolo, "status": "INVALID_TP_LEVEL"}, 400
        else:
            if sl <= entrada:
                return {"error": f"SL para SELL ({sl}) debe ser mayor que la entrada ({entrada}).",
                        "symbol": simbolo, "status": "INVALID_SL_LEVEL"}, 400
            if tp is not None and tp >= entrada:
                return {"error": f"TP para SELL ({tp}) debe ser menor que la entrada ({entrada}).",
                        "symbol": simbolo, "status": "INVALID_TP_LEVEL"}, 400

        # -- 4c. Lote ----------------------------------------------------------
        eff = risk_engine.effective_risk_pct(score, verdict, cfg)
        risk_pct = float(eff.get("risk_pct") or 0.0)
        base_riesgo = self._base_de_riesgo(estado_ftmo)

        lote = volume
        lote_nota: Optional[str] = None
        if lote is None or float(lote) <= 0:
            lote, error_lote = self._lote_por_riesgo(base_riesgo, risk_pct, sl_distance,
                                                     spec, simbolo)
            if error_lote:
                return {"error": error_lote, "symbol": simbolo,
                        "status": "LOT_UNAVAILABLE"}, 400
            riesgo_cash = float(base_riesgo) * risk_pct / 100.0
            lote_nota = ("Lote dimensionado por riesgo ({0}% de {1} = {2:.2f} con un "
                         "SL de {3:.5f}): {4} lotes.".format(
                             risk_pct, base_riesgo, riesgo_cash, sl_distance, lote))
        lote = float(lote)

        # -- 4d. Riesgo por lote, preguntado al bróker -------------------------
        #
        # `fuente_riesgo` se lleva EXPLÍCITO en vez de deducirse de si la cifra
        # existe: deducirlo —"si hay número, es del bróker"— etiquetaría de
        # `broker` una estimación hecha con el tick value. Un campo que miente
        # sobre la procedencia del riesgo es peor que un campo ausente.
        perdida_por_lote = None
        fuente_riesgo: Optional[str] = None
        if self._execution is not None:
            try:
                perdida_por_lote = self._execution.profit_per_lot(simbolo, lote, entrada, sl)
                if perdida_por_lote is not None:
                    fuente_riesgo = "broker"
            except Exception:  # noqa: BLE001 - sin esta cifra se sigue, y se dice
                perdida_por_lote = None
                fuente_riesgo = None
        if perdida_por_lote is None and getattr(spec, "tick_value", 0.0):
            # Reserva: la MISMA fórmula con la que se dimensionó el lote
            # (`lot_calculator.risk_per_lot`), para que el lote y el presupuesto
            # no se calculen con dos cuentas distintas.
            try:
                perdida_por_lote = lot_calculator.risk_per_lot(
                    abs(entrada - sl), self._lot_spec(spec))
            except ValueError as exc:
                perdida_por_lote = None
                avisos.append("No se pudo calcular la pérdida por lote con el spec "
                              "del bróker ({0}); el presupuesto de riesgo NO se ha "
                              "podido comprobar.".format(exc))
            if perdida_por_lote is not None:
                fuente_riesgo = "estimacion"
                avisos.append("Pérdida por lote ESTIMADA con el tick value del "
                              "spec (order_calc_profit no devolvió valor): "
                              "{0:.2f} por lote.".format(perdida_por_lote))

        # -- 4e. validate_entry: R:R, estructura, TTL y presupuesto ------------
        val = risk_engine.validate_entry(
            entry=entrada, sl=sl, target=tp, direction=acc,
            current_price=entrada, invalidate_level=invalidate_level,
            created_at=created_at,
            cfg=cfg, balance=base_riesgo, risk_pct=risk_pct,
            loss_per_lot=perdida_por_lote, lot=lote,
            force=force,
        )
        cal = execution_quality.evaluate(
            entry=entrada, sl=sl, tp=tp, direction=acc,
            point=float(getattr(spec, "point", 0.0) or 0.0),
            planned_entry=planned_entry, sl_distance=sl_distance,
            invalidate_level=invalidate_level,
            bid=precio.get("bid"), ask=precio.get("ask"), cfg=cfg,
            force=force,
        )
        force_skipped.extend(val.get("force_skipped") or [])
        force_skipped.extend(cal.get("force_skipped") or [])

        # `approved` NO es permiso de ejecución: es el veredicto de un gate.
        # La orden sale solo si los DOS lo dicen, y el motivo de cada uno se
        # enseña por separado para que se sepa cuál falló.
        aprobado = bool(val.get("approved")) and bool(cal.get("approved"))
        motivos = list(val.get("reasons") or []) + list(cal.get("reasons") or [])

        fila: Dict[str, Any] = {
            "symbol": simbolo,
            "timeframe": str(timeframe or DEFAULT_TIMEFRAME).upper(),
            "direction": acc,
            "verdict": str(verdict or ""),
            "score": float(score or 0.0),
            "breakdown_json": dict(components or {}),
            "entry": entrada,
            "sl": sl,
            "target": tp,
            "invalidate_level": invalidate_level,
            "validated": 1 if aprobado else 0,
            "reject_reasons": motivos,
            "forced": 1 if force_skipped else 0,
            "risk_state_json": estado_ftmo,
            "trade_result_json": {},
            "context_json": dict(context or {}),
            "source": str(source or ""),
        }

        if not aprobado:
            setup_id = self._registra(fila)
            return {"error": "La orden no pasa el gate de entrada: "
                             + "; ".join(motivos or ["rechazada sin motivo declarado"]),
                    "symbol": simbolo, "status": "GATE_REJECTED",
                    "validated": False,
                    "forced": bool(force_skipped),
                    "force_skipped": force_skipped,
                    "reasons": motivos,
                    "risk_validation": val,
                    "execution_quality": cal,
                    "setup_id": setup_id,
                    "audit_logged": setup_id is not None}, 400

        # -- 5. Auditoría ANTES del envío -------------------------------------
        fila["trade_result_json"] = {"planned_entry": entrada, "planned_sl": sl,
                                     "planned_tp": tp, "volume": lote}
        setup_id = self._registra(fila)

        # -- 6. Envío -----------------------------------------------------------
        if self._execution is None:
            return {"error": "No hay puerto de ejecución cableado: esta instalación "
                             "puede analizar pero no operar.",
                    "symbol": simbolo, "status": "NO_EXECUTION_PORT"}, 503

        magic_efectivo = int(magic if magic is not None else cfg.get("magic", 0) or 0)
        comentario = str(comment if comment is not None
                         else (cfg.get("comment") or "")) or "web"
        resultado = self._execution.send_market_order(
            simbolo, acc, lote, sl=sl, tp=tp, magic=magic_efectivo,
            comment=comentario,
            deviation=int(deviation if deviation is not None
                          else (cfg.get("deviation_points") or 20)),
        )
        self._graba_intentos(setup_id, resultado)
        if resultado.get("ok"):
            if self._prop is not None and hasattr(self._prop, "record_trade"):
                try:
                    self._prop.record_trade(self._account_id)
                except Exception:
                    pass

        cuerpo: Dict[str, Any] = {
            "ok": bool(resultado.get("ok")),
            "symbol": simbolo,
            "action": acc,
            "volume": resultado.get("volume", lote),
            "requested_volume": lote,
            "entry": entrada,
            "sl": sl,
            "tp": tp,
            "fill_price": resultado.get("fill_price") or resultado.get("price"),
            "deal": resultado.get("deal"),
            "order": resultado.get("order"),
            "retcode": resultado.get("retcode"),
            "filling": resultado.get("filling"),
            "status": resultado.get("status"),
            "setup_id": setup_id,
            "audit_logged": setup_id is not None,
            "forced": bool(force_skipped),
            "force_skipped": force_skipped,
            "risk": {"risk_pct": risk_pct, "reason": eff.get("reason"),
                     "sl_distance": sl_distance, "sl_distance_source": origen_sl,
                     "tp_distance": tp_distance,
                     "loss_per_lot": perdida_por_lote,
                     "loss_per_lot_source": fuente_riesgo},
            # En el rechazo se necesita saber cuál de los dos falló; en el éxito
            # sirve para comprobar que la orden que salió pasó la puerta que se
            # cree que pasó, y para leer los `unknown` sin ir a la base de datos.
            "risk_validation": val,
            "execution_quality": {k: cal.get(k) for k in
                                  ("verdict", "approved", "reasons", "unknown",
                                   "force_skipped",
                                   "spread_points", "spread_share_of_sl",
                                   "slippage_share_of_sl",
                                   "invalidation_share_of_sl", "rr_actual", "rr_planned")},
            "attempts": resultado.get("attempts") or [],
        }
        if avisos:
            cuerpo["warnings"] = avisos
        if lote_nota:
            cuerpo.setdefault("warnings", []).append(lote_nota)
        if resultado.get("volume_note"):
            cuerpo.setdefault("warnings", []).append(resultado["volume_note"])
        if not resultado.get("ok"):
            cuerpo["error"] = resultado.get("error")

        status = 200 if resultado.get("ok") else _status_de_ejecucion(resultado.get("status"))
        return cuerpo, status

    # -- cierre -----------------------------------------------------------------

    @_con_cuenta
    def close_position(self, ticket: Optional[int] = None,
                       symbol: Optional[str] = None) -> Tuple[Dict[str, Any], int]:
        """Cierra la posición por ticket (o por símbolo si no hay ticket).

        El cierre NO pasa por `symbols_allow` ni por el gate FTMO: una lista blanca
        de símbolos para ABRIR no puede impedir cerrar lo que ya está abierto, y un
        tope de riesgo del día no puede dejar una posición viva sin forma de salir.
        Bloquear un cierre por cualquiera de los dos dejaría la posición abierta y
        el único remedio sería editar la config con la posición viva.

        Aunque la revisión de FTMO la haría un gate de apertura, este cierre SÍ
        responde al hecho de que la cuenta ya no tiene por qué estar "tracked":
        cerrar reduce riesgo, no lo abre.
        """
        simbolo = str(symbol or "").strip().upper()
        if not ticket and not simbolo:
            return {"error": "Falta el ticket de la posición a cerrar."}, 400

        if self._execution is None:
            return {"error": "No hay puerto de ejecución cableado: esta instalación "
                             "puede analizar pero no operar.",
                    "status": "NO_EXECUTION_PORT"}, 503

        cfg = self._cfg()
        resultado = self._execution.close_position(
            ticket=int(ticket) if ticket else None,
            symbol=simbolo or None,
            magic=int(cfg.get("magic", 0) or 0),
            deviation=int(cfg.get("deviation_points") or 20),
        )
        if resultado.get("ok"):
            return {"ok": True, **resultado}, 200

        estado = resultado.get("status")
        if estado == "no_position":
            return {"error": resultado.get("error"), "status": estado,
                    "ticket": ticket, "symbol": simbolo or None}, 404
        if estado == "sin_terminal":
            return {"error": resultado.get("error"), "status": estado}, 503
        if estado == "symbol_not_found":
            return {"error": resultado.get("error"), "status": estado}, 404
        return {"error": resultado.get("error"), "status": estado,
                "ticket": resultado.get("ticket"), "symbol": resultado.get("symbol"),
                "retcode": resultado.get("retcode"), "filling": resultado.get("filling"),
                "attempts": resultado.get("attempts") or []}, 400

    # -- modificación de stops ---------------------------------------------------

    @_con_cuenta
    def modify_stop(self, ticket: Optional[int], sl: Optional[float] = None,
                    tp: Optional[float] = None) -> Tuple[Dict[str, Any], int]:
        """`(cuerpo, status)` moviendo el stop (y/o el TP) de una posición ABierta.

        **No pasa por las puertas de apertura, y no es un hueco.** La lista blanca
        y el riesgo del día responden a la pregunta "¿puedo ABRIR riesgo?"; mover
        el stop de una posición que ya existe no abre nada: el riesgo de esa
        posición ya está comprometido y mover su stop hacia atrás solo puede
        EMPEORARLO. Bloquear un trailing por un tope de riesgo dejaría el stop donde
        está justo cuando más se necesita. Las garantías son, por tanto, DOS y están
        antes de esta llamada: el ratchet "nunca afloja" del cálculo de salida, y
        aquí el ticket y los niveles tal cual llegan.

        Un `sl=None` (o `tp=None`) significa "no tocar ese nivel": el bróker
        conserva el que ya tiene. Quien quita un nivel pasa `0.0`, que es la
        forma en que MT5 lo entiende.
        """
        if not ticket:
            return {"error": "Falta el ticket de la posición a modificar."}, 400
        if sl is None and tp is None:
            return {"error": "No hay nada que modificar: pasa sl y/o tp "
                             "(None deja el nivel como está)."}, 400
        if self._execution is None:
            return {"error": "No hay puerto de ejecución cableado: esta instalación "
                             "puede analizar pero no operar.",
                    "status": "NO_EXECUTION_PORT"}, 503

        fn = getattr(self._execution, "modify_position", None)
        if not callable(fn):
            return {"error": "El puerto de ejecución no sabe modificar posiciones "
                             "(no implementa modify_position).",
                    "status": "NO_EXECUTION_PORT"}, 503
        try:
            resultado = fn(int(ticket), sl=sl, tp=tp)
        except Exception as exc:  # noqa: BLE001 - un error de puente se reporta, no se propaga
            return {"error": "No se pudo modificar la posición {0}: {1}".format(ticket, exc),
                    "status": "MODIFY_FAILED", "ticket": int(ticket)}, 503

        if resultado.get("ok"):
            return {"ok": True, **resultado}, 200

        estado = str(resultado.get("status") or "rejected")
        status_http = {"no_position": 404, "symbol_not_found": 404,
                       "sin_terminal": 503, "sin_respuesta": 503}.get(estado, 400)
        cuerpo = {"error": resultado.get("error")
                  or "El bróker no aceptó la modificación.", "status": estado,
                  "ticket": resultado.get("ticket", int(ticket)),
                  "symbol": resultado.get("symbol"),
                  "retcode": resultado.get("retcode"),
                  "attempts": resultado.get("attempts") or []}
        return cuerpo, status_http

    # -- gestión parcial escalada -------------------------------------------------

    def _posicion_por_ticket(self, ticket: int
                             ) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[Dict[str, Any], int]]]:
        """`(fila, None)` o `(None, (cuerpo_error, status))`.

        Busca la posición ABierta por ticket en el puerto de ejecución. El puerto
        no expone una lectura por ticket, así que se filtra la lista: son pocas
        posiciones abiertas y el ticket es el identificador real (en una cuenta
        hedging hay varias del mismo símbolo).
        """
        if self._execution is None:
            return None, ({"error": "No hay puerto de ejecución cableado: esta "
                                    "instalación puede analizar pero no operar.",
                           "status": "NO_EXECUTION_PORT"}, 503)
        fn = getattr(self._execution, "positions", None)
        if not callable(fn):
            return None, ({"error": "El puerto de ejecución no sabe listar "
                                    "posiciones.", "status": "NO_EXECUTION_PORT"}, 503)
        try:
            filas = fn() or []
        except Exception as exc:  # noqa: BLE001 - el motivo viaja en `error`
            return None, ({"error": "No se pudieron leer las posiciones abiertas: "
                                    "{0}".format(exc), "status": "POSITIONS_FAILED"}, 503)
        objetivo = int(ticket)
        for fila in filas:
            try:
                if int(fila.get("ticket") or 0) == objetivo:
                    return dict(fila), None
            except (TypeError, ValueError):
                continue
        return None, ({"error": "No hay posición abierta con ticket {0}."
                                .format(objetivo), "status": "no_position",
                       "ticket": objetivo}, 404)

    def _spec_de_gestion(self, symbol: str) -> Tuple[float, float]:
        """`(volume_min, volume_step)` del bróker, con defaults honestos.

        Un mercado que no publica spec no puede decidir el redondeo del lote:
        se cae a 0.01/0.01, que es el mínimo común del forex, y la política de
        salida (`partial_close_volume`) rechazará cualquier cierre que no encaje.
        """
        try:
            spec = self._market.spec(symbol) if self._market is not None else None
        except Exception:  # noqa: BLE001 - sin spec se opera con los defaults
            spec = None
        if spec is None:
            return 0.01, 0.01
        try:
            vmin = float(getattr(spec, "volume_min", 0.01) or 0.01)
        except (TypeError, ValueError):
            vmin = 0.01
        try:
            vstep = float(getattr(spec, "volume_step", 0.01) or 0.01)
        except (TypeError, ValueError):
            vstep = 0.01
        return vmin, vstep

    def _estado_posicion(self, ticket: int) -> Dict[str, Any]:
        """`position_state` del ticket, o `{}` si no hay store o no existe."""
        fn = getattr(self._store, "get_position_state", None)
        if self._store is None or not callable(fn):
            return {}
        try:
            return fn(int(ticket)) or {}
        except Exception:  # noqa: BLE001 - sin estado se infiere del SL actual
            return {}

    def _guarda_estado(self, ticket: int, patch: Dict[str, Any]) -> None:
        fn = getattr(self._store, "update_position_state", None)
        if self._store is None or not callable(fn):
            return
        try:
            fn(int(ticket), patch)
        except Exception:  # noqa: BLE001 - la auditoría nunca tumba la ejecución
            return

    @_con_cuenta
    def management_view(self, ticket: int) -> Tuple[Dict[str, Any], int]:
        """`(vista, 200)` o `(error, status)`: QUÉ se puede aplicar a `ticket`.

        Consulta pura: no mueve dinero, no escribe estado. Es lo que el panel
        mira (vía GET) antes de habilitar el botón, y lo que `apply_management`
        vuelve a calcular al instante de ejecutar — la vista del panel puede
        estar segundos atrás; la decisión se toma con los números de AHORA.
        """
        try:
            objetivo = int(ticket)
        except (TypeError, ValueError):
            return {"error": "Ticket inválido: {0!r}".format(ticket)}, 400

        fila, fallo = self._posicion_por_ticket(objetivo)
        if fallo:
            return fallo

        cfg = self._cfg()
        estado = self._estado_posicion(objetivo)
        vmin, vstep = self._spec_de_gestion(fila.get("symbol") or "")
        vista = exit_policy.management_view(fila, estado, cfg, vmin, vstep)
        return vista, 200

    @_con_cuenta
    def apply_management(self, ticket: int, force: bool = False
                         ) -> Tuple[Dict[str, Any], int]:
        """`(cuerpo, status)`: aplica la gestión parcial escalada a `ticket`.

        El orden de los pasos es la garantía y no se reordena:

        1. **Parcial primero.** Cerrar el tramo asegurado y DESPUÉS mover el SL
           es lo que deja la posición restante sin riesgo. Al revés —mover el SL
           a BE antes— hace que el parcial se dispare solo si el precio sigue a
           favor; si se queda en medio, la posición queda a BE sin haber
           asegurado nada.
        2. **Si el parcial FALLA, no se toca el SL.** Un parcial rechazado deja
           la posición completa; mover igualmente el SL a BE aflojaría el stop
           de una posición que sigue arriesgando el lote entero.
        3. **Ratchet en el SL.** El BE se propone, pero `ratchet_sl` decide: un
           stop ya más cerca del precio (movido a mano) no se reemplaza por uno
           peor.
        4. **Idempotencia por estado.** `partial_taken` en `position_state`
           hace que una segunda llamada salte el parcial y solo asegure el BE:
           doble clic, o un reintento tras un timeout, no cierra dos veces.

        `force=True` salta el gate de "¿lleva ya 1:2R?" SOLO para la parte de
        break-even y runner: nunca cierra el parcial antes de su ratio. Sirve
        para proteger una posición a mano cuando el operador decide que el BE
        es prioridad, y es una decisión humana explícita (D-005).
        """
        try:
            objetivo = int(ticket)
        except (TypeError, ValueError):
            return {"error": "Ticket inválido: {0!r}".format(ticket)}, 400

        fila, fallo = self._posicion_por_ticket(objetivo)
        if fallo:
            return fallo

        cfg = self._cfg()
        estado = self._estado_posicion(objetivo)
        vmin, vstep = self._spec_de_gestion(fila.get("symbol") or "")
        vista = exit_policy.management_view(fila, estado, cfg, vmin, vstep)

        partial_taken = bool(vista.get("partial_taken"))
        if not vista.get("can_apply") and not force:
            return {"error": "La gestión no se puede aplicar todavía.",
                    "status": "not_ready", "ticket": objetivo,
                    "reasons": vista.get("reasons") or [],
                    "management": vista}, 409
        if not vista.get("can_apply") and force and not vista.get("r_original"):
            return {"error": "Sin R original no hay niveles que mover: pásalo a "
                             "mano o deja que la posición alcance su SL inicial.",
                    "status": "not_ready", "ticket": objetivo,
                    "reasons": vista.get("reasons") or []}, 409

        # Memoria de la tesis: se guarda ANTES de mover dinero, con el R
        # original, que tras el BE ya no es recuperable del bróker.
        if self._store is not None and callable(
                getattr(self._store, "upsert_position_state", None)):
            try:
                self._store.upsert_position_state({
                    "ticket": objetivo, "symbol": fila.get("symbol") or "",
                    "direction": vista.get("direction") or "",
                    "price_open": vista.get("price_open"),
                    "volume_original": vista.get("volume"),
                    "r_original": vista.get("r_original"),
                })
            except Exception:  # noqa: BLE001 - la auditoría nunca tumba la ejecución
                pass

        cuerpo: Dict[str, Any] = {"ok": False, "ticket": objetivo,
                                  "symbol": fila.get("symbol"),
                                  "partial": None, "breakeven": None}

        # 1) Parcial (solo si aún no se tomó, el precio YA llegó a su ratio y
        #    hay lote que cerrar: `force` nunca cierra el parcial antes de tiempo).
        if not partial_taken and vista.get("partial_ready"):
            vol_parcial = vista.get("partial_volume")
            if vol_parcial:
                cierre = self._execution.close_position(
                    ticket=objetivo, volume=float(vol_parcial),
                    magic=int(cfg.get("magic", 0) or 0),
                    deviation=int(cfg.get("deviation_points") or 20),
                )
                cuerpo["partial"] = cierre
                if not cierre.get("ok"):
                    cuerpo["error"] = ("El parcial no se pudo cerrar: {0} "
                                       .format(cierre.get("error")))
                    cuerpo["status"] = cierre.get("status") or "partial_failed"
                    # Regla 2: sin parcial no se toca el SL. El estado NO marca
                    # `partial_taken`, así que el siguiente intento lo reintentará.
                    self._registra_management(objetivo, fila, vista, cuerpo)
                    return cuerpo, _status_de_ejecucion(cierre.get("status"))
                self._guarda_estado(objetivo, {
                    "partial_taken": 1,
                    "partial_volume": float(vol_parcial),
                    "partial_deal": cierre.get("deal"),
                    "status": "partial",
                })
                cuerpo["partial_taken"] = True
            else:
                # Posición demasiado pequeña para partirse: la política lo
                # explica en `partial_reason` y aquí solo se aplica BE.
                cuerpo["partial"] = {"skipped": True,
                                     "reason": vista.get("partial_reason")}

        # 2) Break-even + runner (ratchet "nunca afloja" en el SL).
        be_level = vista.get("be_level")
        if be_level is None:
            cuerpo["error"] = ("No hay break-even calculable (sin entrada "
                               "utilizable).")
            cuerpo["status"] = "no_be_level"
            self._registra_management(objetivo, fila, vista, cuerpo)
            return cuerpo, 400
        sl_final = exit_policy.ratchet_sl(vista.get("direction") or "",
                                          _float_or_none(fila.get("sl")),
                                          be_level)
        tp_final = vista.get("runner_level") if vista.get("set_runner_tp") else None
        mod = self._execution.modify_position(objetivo, sl=sl_final, tp=tp_final)
        cuerpo["breakeven"] = mod
        if not mod.get("ok"):
            cuerpo["error"] = ("El break-even no se pudo aplicar: {0}"
                               .format(mod.get("error")))
            cuerpo["status"] = mod.get("status") or "be_failed"
            self._registra_management(objetivo, fila, vista, cuerpo)
            status_http = {"no_position": 404, "symbol_not_found": 404,
                           "sin_terminal": 503, "sin_respuesta": 503}.get(
                               str(mod.get("status") or ""), 400)
            return cuerpo, status_http

        self._guarda_estado(objetivo, {
            "be_applied": 1, "be_level": sl_final,
            "runner_level": tp_final, "status": "runner",
        })
        cuerpo["ok"] = True
        cuerpo["status"] = "managed"
        cuerpo["be_level"] = sl_final
        cuerpo["runner_level"] = tp_final
        cuerpo["partial_volume"] = vista.get("partial_volume")
        cuerpo["r_original"] = vista.get("r_original")
        self._registra_management(objetivo, fila, vista, cuerpo)
        return cuerpo, 200

    def _registra_management(self, ticket: int, fila: Dict[str, Any],
                             vista: Dict[str, Any], cuerpo: Dict[str, Any]) -> None:
        """Auditoría de la gestión en `setup_log`, con `source="gestion_parcial"`.

        Una gestión es una decisión que mueve dinero: tiene que aparecer en el
        mismo post-mortem que las aperturas, con el motivo y el resultado. Un
        fallo de auditoría no tumba la gestión (mismo trato que el resto).
        """
        self._registra({
            "symbol": fila.get("symbol") or "",
            "timeframe": DEFAULT_TIMEFRAME,
            "direction": vista.get("direction") or "",
            "verdict": "GESTION_PARCIAL",
            "score": 0.0,
            "entry": vista.get("price_open"),
            "sl": cuerpo.get("be_level"),
            "target": vista.get("runner_level"),
            "validated": 1,
            "source": "gestion_parcial",
            "context_json": {
                "ticket": int(ticket),
                "r_original": vista.get("r_original"),
                "r_actual": vista.get("r_actual"),
                "partial_ratio_r": vista.get("partial_ratio_r"),
                "runner_ratio_r": vista.get("runner_ratio_r"),
                "partial_volume": vista.get("partial_volume"),
                "partial_reason": vista.get("partial_reason"),
                "partial_taken": bool(cuerpo.get("partial_taken")),
                "force": False,
                "result": {k: cuerpo.get(k) for k in
                           ("ok", "status", "error", "be_level", "runner_level")},
            },
        })

    # -- posiciones --------------------------------------------------------------

    @_con_cuenta
    def positions(self, magic: Optional[int] = None,
                  with_management: bool = False
                  ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """`(filas, error)`: posiciones abiertas, filtradas por `magic` si se pasa.

        `([], None)` es "no hay posiciones (o ninguna de este magic)", que NO es
        un error: es el caso normal de un trailing sin nada que mover. El error,
        cuando existe, distingue las dos formas de no poder contestar —sin puerto
        cableado (la instalación analiza pero no opera)— y un puerto que no sabe
        listar posiciones, porque el que llama decide diferente según cuál sea.

        `with_management=True` adjunta a cada fila un bloque `management` con la
        vista de gestión parcial (R actual, si ya se tomó el parcial, si el BE
        está aplicado, y el nivel del runner). Es una consulta por fila: un fallo
        en UNA posición no puede tumbar el listado completo, así que cada fila
        fallida lleva su `management_error` y el resto se sirve igual.
        """
        if self._execution is None:
            return None, ("No hay puerto de ejecución cableado: esta instalación "
                          "puede analizar pero no opera.")
        fn = getattr(self._execution, "positions", None)
        if not callable(fn):
            return None, "El puerto de ejecución no sabe listar posiciones (no implementa positions())."
        try:
            filas = fn()
        except Exception as exc:  # noqa: BLE001 - el motivo viaja en `error`, no revienta
            return None, "No se pudieron leer las posiciones abiertas: {0}".format(exc)
        if not isinstance(filas, (list, tuple)):
            return None, "El puerto de ejecución devolvió las posiciones en un formato desconocido."
        salidas = [dict(f) for f in filas]
        if magic is not None:
            objetivo = int(magic)
            salidas = [f for f in salidas if int(f.get("magic") or 0) == objetivo]
        if with_management:
            cfg = self._cfg()
            for fila in salidas:
                try:
                    ticket = int(fila.get("ticket") or 0)
                    estado = self._estado_posicion(ticket)
                    vmin, vstep = self._spec_de_gestion(fila.get("symbol") or "")
                    fila["management"] = exit_policy.management_view(
                        fila, estado, cfg, vmin, vstep)
                except Exception as exc:  # noqa: BLE001 - una fila rota no tumba la lista
                    fila["management"] = None
                    fila["management_error"] = str(exc)
        return salidas, None


def _float_or_none(value: Any) -> Optional[float]:
    """`float(value)` o `None` si no es un número utilizable (incluido 0.0 → 0.0)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status_de_ejecucion(estado: Optional[str]) -> int:
    """Traduce el estado del bróker a un status HTTP con sentido.

    No se colapsa todo a 400: un `sin_terminal` es 503 (reintentable) y un
    símbolo inexistente es 404. Un 400 para "no hay terminal" hace que un cliente
    que reintenta solo ante 5xx no reintente nunca, y la orden se pierde en el
    primer corte de conexión.
    """
    return {
        "sin_terminal": 503,
        "symbol_not_found": 404,
        "sin_precio": 503,
        "volume_invalido": 400,
        "rejected": 400,
    }.get(str(estado or ""), 400)


def execution_from(runtime_rt: Any) -> ExecutionService:
    """Construye el servicio con las piezas del runtime ya montadas."""
    return ExecutionService(
        market=runtime_rt.market,
        store=runtime_rt.store,
        execution=getattr(runtime_rt, "execution", None),
        prop=getattr(runtime_rt, "prop", None),
        account_id=getattr(runtime_rt, "account_id", None),
    )


__all__ = [
    "DEFAULT_TIMEFRAME",
    "ExecutionService",
    "execution_from",
]
