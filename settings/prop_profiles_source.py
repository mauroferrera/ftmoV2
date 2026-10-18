"""La mitad de `config/prop_profiles.yaml` que toca el fichero.

`core/prop_profiles.py` sabe qué es un mapa de perfiles válido y cómo se resuelve
un login; este módulo sabe dónde está el archivo y cómo se lee. Es el mismo reparto
que `settings/strategy_source.py` con `strategy.yaml`, y por el mismo motivo:
`core/` no puede abrir ficheros (test de pureza) y meter la lectura aquí ataría
`store` a una fuente de verdad que no le toca.

Igual que el mapa de estrategias, el fichero NO es obligatorio: sin él, el registro
queda VACÍO (`empty_profiles()`), que es el estado "sin configurar" —ningún login
resuelve a perfil, y con el shell prop encendido eso bloquea (fail-closed) en vez
de dimensionar a ciegas. Un fichero mal formado SÍ revienta
(`PropProfilesError`): un tope de DD equivocado no da un error visible, da una
cuenta reventada.

Nombres públicos para `store`: `load` e `invalidate`, con la misma firma que los
otros sources.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from core import paths as _paths
from core.prop_profiles import (
    PropProfiles,
    PropProfilesError,
    empty_profiles,
    parse_profiles,
)

PROP_PROFILES_PATH = _paths.PROP_PROFILES_PATH

_cache: dict = {}
_cache_lock = threading.Lock()


# ============================================================
# Disco
# ============================================================

def _leer() -> str | None:
    """El texto del fichero, o None si no existe."""
    if not os.path.exists(PROP_PROFILES_PATH):
        return None
    with open(PROP_PROFILES_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def _firma() -> tuple | None:
    """`(mtime_ns, tamaño)` del fichero, o None si no está. Su cambio invalida.

    La firma incluye las variables de entorno que MAPEAN cuentas (`MT5_LOGIN`,
    `MT5_ACCOUNTS`, `FTMO_PROFILE` y `FTMO_PROFILE_<LOGIN>`): si el operador
    añade una cuenta al `.env`, la caché no puede seguir sirviendo el mapa viejo.
    """
    try:
        st = os.stat(PROP_PROFILES_PATH)
        base = (st.st_mtime_ns, st.st_size)
    except OSError:
        base = None
    env_login = os.getenv("MT5_LOGIN", "").strip()
    env_profile = os.getenv("FTMO_PROFILE", "").strip()
    env_accounts = _logins_env()
    env_por_cuenta = tuple(
        sorted((k, v) for k, v in os.environ.items() if k.startswith("FTMO_PROFILE_"))
    )
    return (base, env_login, env_profile, env_accounts, env_por_cuenta)


def _logins_env() -> tuple:
    """Los logins declarados en `MT5_ACCOUNTS` (coma-separados), normalizados.

    Es la lista de cuentas para la UI cuando no se quieren versionar los números
    en `prop_profiles.yaml`: el `.env` no se versiona.
    """
    crudo = os.getenv("MT5_ACCOUNTS", "")
    return tuple(sorted({p.strip() for p in crudo.split(",") if p.strip()}))


def _perfil_de_cuenta(perfiles: PropProfiles, login: str) -> str:
    """El perfil de una cuenta: env por cuenta > YAML > `FTMO_PROFILE` > default.

    El orden permite cuatro cuentas con perfiles distintos (`FTMO_PROFILE_<LOGIN>`)
    sin tocar el YAML, y una sola cuenta sigue funcionando como siempre.
    """
    clave = str(login).strip()
    por_cuenta = os.getenv("FTMO_PROFILE_" + clave, "").strip()
    if por_cuenta:
        return por_cuenta
    return (perfiles.accounts.get(clave)
            or os.getenv("FTMO_PROFILE", "").strip()
            or "ftmo_2step_25k")


# ============================================================
# Cache
# ============================================================

def _cargada() -> bool:
    if not _cache:
        return False
    return _cache.get("firma") == _firma()


def invalidate() -> None:
    """Olvida los perfiles cacheados. La próxima lectura vuelve al fichero."""
    with _cache_lock:
        _cache.clear()


# ============================================================
# Lectura
# ============================================================

def load(force: bool = False) -> PropProfiles:
    """El registro de perfiles por cuenta, cacheado.

    Fichero ausente -> registro vacío (nada resuelve). Fichero mal formado ->
    `PropProfilesError`: nunca un default silencioso, porque el default de un
    perfil FTMO son topes que nadie eligió.
    """
    if not force and _cargada():
        return _cache["perfiles"]
    texto = _leer()
    perfiles = empty_profiles() if texto is None else parse_profiles(_safe_load(texto))

    # Mapeo de cuenta desde variable de entorno (.env) para no exponer credenciales
    env_login = os.getenv("MT5_LOGIN", "").strip()
    if env_login:
        perfil = _perfil_de_cuenta(perfiles, env_login)
        if perfil in perfiles.profiles:
            perfiles.accounts[env_login] = perfil

    # Varias cuentas: `MT5_ACCOUNTS` lista logins y `FTMO_PROFILE_<LOGIN>` fija el
    # perfil de cada una; sin perfil propio se usa el genérico (`FTMO_PROFILE`) y,
    # si tampoco hay, el de la cuenta única (`ftmo_2step_25k`).
    for login in _logins_env():
        perfil = _perfil_de_cuenta(perfiles, login)
        if perfil in perfiles.profiles:
            perfiles.accounts[login] = perfil

    with _cache_lock:
        _cache.clear()
        _cache["perfiles"] = perfiles
        _cache["firma"] = _firma()
    return perfiles


def _safe_load(texto: str) -> Any:
    """`yaml.safe_load` traduciendo el fallo de sintaxis a `PropProfilesError`."""
    import yaml  # noqa: PLC0415 - tardío: solo el loader necesita PyYAML

    try:
        doc = yaml.safe_load(texto)
    except yaml.YAMLError as exc:
        raise PropProfilesError(
            "prop_profiles.yaml no es un YAML válido: {0}".format(exc)
        ) from exc
    return doc


__all__ = [
    "PROP_PROFILES_PATH",
    "invalidate",
    "load",
]
