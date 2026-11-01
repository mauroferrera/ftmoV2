"""Control de exposición por clusters de divisas y riesgo agregado (P1 - Audit #8).

Módulo PURO: no importa MT5, red ni base de datos.
Recibe posiciones abiertas y configuraciones, y evalúa:
1. Límite de posiciones abiertas por símbolo (máx 1).
2. Límite de posiciones abiertas por FACTOR DIRECCIONAL (USD_long, JPY_short…).
3. Límite de riesgo abierto total en múltiplos R (ej. 1.5R).

Por qué factor direccional y no solo divisa (M25)
-------------------------------------------------
Contar "cuántas posiciones tocan USD" es contar correlación a medias: dos
operaciones pueden compartir divisa y estar en lados OPUESTOS del mismo riesgo
(`EURUSD` BUY y `USDCHF` BUY son ambas "USD corto"; `EURUSD` BUY y `EURUSD` SELL
no son la misma apuesta). Con el cupo por divisa y tope 1, una sola posición con
USD bloqueaba 8 de los 12 símbolos; con factor y cupo 2 se puede diversificar
sin volver a apostar dos veces a lo mismo. El mapa por divisa se conserva para
cuando la dirección no se conoce.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

# Clusters macro por defecto para los 12 símbolos de FTMO Forex/Metales.
# Cada símbolo mapea a las divisas que componen el par (base y quote) o activo.
DEFAULT_CLUSTERS: Dict[str, List[str]] = {
    "AUD": ["AUDCAD", "AUDCHF", "AUDJPY", "AUDNZD", "AUDUSD"],
    "USD": ["EURUSD", "GBPUSD", "USDCAD", "USDCHF", "USDJPY", "AUDUSD", "XAUUSD"],
    "JPY": ["USDJPY", "EURJPY", "AUDJPY"],
    "XAU": ["XAUUSD"],
    "EUR": ["EURUSD", "EURJPY"],
    "GBP": ["GBPUSD"],
    "CAD": ["AUDCAD", "USDCAD"],
    "CHF": ["AUDCHF", "USDCHF"],
    "NZD": ["AUDNZD"],
}


def get_symbol_clusters(
    symbol: str,
    custom_clusters: Optional[Dict[str, List[str]]] = None,
) -> List[str]:
    """Identifica todos los clusters a los que pertenece un símbolo.

    Busca en `custom_clusters` o `DEFAULT_CLUSTERS`. Si el símbolo no está en
    ningún cluster predefinido, descompone símbolos forex estándar de 6 caracteres
    en su divisa base (tres primeros) y divisa cotizada (tres últimos).
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return []

    clusters_map = custom_clusters if custom_clusters is not None else DEFAULT_CLUSTERS
    encontrados: Set[str] = set()

    for cluster_name, simbolos in clusters_map.items():
        simbolos_upper = {str(s).strip().upper() for s in simbolos}
        if sym in simbolos_upper:
            encontrados.add(cluster_name)

    # Si no se encontró en el mapa y es un par de 6 caracteres (ej. NZDUSD)
    if not encontrados and len(sym) == 6:
        base, quote = sym[:3], sym[3:]
        encontrados.add(base)
        encontrados.add(quote)

    return sorted(encontrados)


def directional_factors(symbol: str, direction: str) -> List[str]:
    """Los factores direccionales de una operación: `["EUR_long", "USD_short"]`.

    Un `EURUSD` BUY es EUR largo y USD corto; el mismo par en SELL es lo
    contrario. Eso es lo que hace comparables dos trades: dos posiciones que
    comparten factor están apostando a lo MISMO y suman riesgo, aunque sean
    pares distintos.

    Los símbolos con sufijo (`EURUSD.a`, `XAUUSD.m`) no se descomponen: sin la
    garantía de base/cotizada de 3+3, inventar factores sería peor que no
    medirlos. `direction` distinto de BUY/SELL tampoco produce factores.
    """
    sym = str(symbol or "").strip().upper()
    dir_clean = str(direction or "").strip().upper()
    if len(sym) != 6 or dir_clean not in ("BUY", "SELL"):
        return []
    base, quote = sym[:3], sym[3:]
    lado = "long" if dir_clean == "BUY" else "short"
    opuesto = "short" if lado == "long" else "long"
    return sorted({f"{base}_{lado}", f"{quote}_{opuesto}"})


def calculate_open_risk_r(pos: Dict[str, Any]) -> float:
    """Calcula el riesgo abierto en R que representa una posición viva.

    Si la posición ya tiene el Stop Loss protegiendo el precio de apertura
    (Break-Even o beneficio), el riesgo asumido sobre el capital es 0.0R:
    libera el cupo de riesgo para nuevas oportunidades.

    Si no ha alcanzado BE, toma el campo explícito `risk_r` si existe, o 1.0R por defecto.
    """
    sl = float(pos.get("sl") or 0.0)
    price_open = float(pos.get("price_open") or 0.0)
    pos_type = str(pos.get("type") or "BUY").upper()

    if sl > 0 and price_open > 0:
        if pos_type == "BUY" and sl >= price_open:
            return 0.0
        if pos_type == "SELL" and sl <= price_open:
            return 0.0

    explicit_r = pos.get("risk_r")
    if explicit_r is not None:
        try:
            val = float(explicit_r)
            return max(0.0, val)
        except (TypeError, ValueError):
            pass

    return 1.0


def check_exposure_limits(
    open_positions: List[Dict[str, Any]],
    new_symbol: str,
    new_risk_r: float = 1.0,
    *,
    max_positions_per_symbol: int = 1,
    max_positions_per_cluster: int = 2,
    max_total_open_risk_r: float = 1.5,
    clusters_map: Optional[Dict[str, List[str]]] = None,
    new_direction: Optional[str] = None,
    max_positions_per_factor: Optional[int] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Evalúa si una nueva orden viola los límites de exposición de capital.

    Devuelve `(permitido, motivo_error, codigo_error)`.

    Verifica de forma determinista:
    1. Límite de posiciones abiertas por símbolo: no duplicar el mismo par.
    2. Límite por FACTOR DIRECCIONAL cuando se conoce `new_direction` (M25):
       no acumular la misma apuesta (USD_long, AUD_short…). Sin dirección se
       conserva el conteo por divisa, que es lo único que se puede afirmar.
    3. Límite de riesgo total abierto en R: la suma de R en riesgo no debe
       superar el tope.

    `max_positions_per_factor` manda sobre `max_positions_per_cluster` cuando
    se pasa: el cupo por factor es más granular y por eso tiene su propia
    perilla, sin romper el contrato de quien solo conoce el mapa por divisa.
    """
    sym = str(new_symbol or "").strip().upper()
    if not sym:
        return False, "Falta el símbolo para evaluar exposición.", "INVALID_SYMBOL"

    posiciones = open_positions or []

    # 1. Posiciones abiertas por símbolo
    symbol_positions = [
        p for p in posiciones
        if str(p.get("symbol") or "").strip().upper() == sym
    ]
    if len(symbol_positions) >= max_positions_per_symbol:
        return (
            False,
            f"Límite de posiciones para {sym} alcanzado ({len(symbol_positions)}/{max_positions_per_symbol} abiertas).",
            "MAX_POSITIONS_PER_SYMBOL_REACHED",
        )

    # 2. Posiciones abiertas por factor direccional (o por divisa si no hay lado)
    if new_direction:
        cupo = (max_positions_per_factor
                if max_positions_per_factor is not None
                else max_positions_per_cluster)
        for factor in directional_factors(sym, new_direction):
            count_factor = sum(
                1 for p in posiciones
                if factor in directional_factors(
                    str(p.get("symbol") or ""), str(p.get("type") or ""))
            )
            if count_factor >= cupo:
                return (
                    False,
                    f"Límite de exposición para el factor '{factor}' alcanzado "
                    f"({count_factor}/{cupo} posiciones abiertas en la misma apuesta).",
                    "CLUSTER_EXPOSURE_REACHED",
                )
    else:
        new_clusters = get_symbol_clusters(sym, custom_clusters=clusters_map)
        for cl in new_clusters:
            # Contar cuántas posiciones abiertas pertenecen a este cluster
            count_in_cluster = 0
            for p in posiciones:
                p_sym = str(p.get("symbol") or "").strip().upper()
                if not p_sym:
                    continue
                p_clusters = get_symbol_clusters(p_sym, custom_clusters=clusters_map)
                if cl in p_clusters:
                    count_in_cluster += 1

            if count_in_cluster >= max_positions_per_cluster:
                return (
                    False,
                    f"Límite de exposición para cluster '{cl}' alcanzado "
                    f"({count_in_cluster}/{max_positions_per_cluster} posiciones abiertas en el factor).",
                    "CLUSTER_EXPOSURE_REACHED",
                )

    # 3. Riesgo total abierto en R
    current_total_r = sum(calculate_open_risk_r(p) for p in posiciones)
    projected_total_r = round(current_total_r + float(new_risk_r), 4)

    if projected_total_r > max_total_open_risk_r + 1e-6:
        return (
            False,
            f"Riesgo abierto total ({projected_total_r:.2f}R) excedería el máximo permitido "
            f"({max_total_open_risk_r:.2f}R). Riesgo actual abierto: {current_total_r:.2f}R.",
            "MAX_OPEN_RISK_REACHED",
        )

    return True, None, None

