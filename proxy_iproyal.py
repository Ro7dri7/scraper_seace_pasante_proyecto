"""
Cliente de proxy residencial IPRoyal (Pay As You Go, país PE).

URL de autenticación:
  http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}

Si PROXY_COUNTRY=PE y el usuario no trae ya `_country-pe`, se anexa
(documentación IPRoyal: user_country-pe).
"""
from __future__ import annotations

import os
from urllib.parse import quote

from supabase_sync import load_env

load_env()


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    return default if val is None else val.strip()


def proxy_habilitado() -> bool:
    raw = _env("PROXY_ENABLED", "1").lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(_env("PROXY_HOST") and _env("PROXY_PORT"))


def proxy_user() -> str:
    user = _env("PROXY_USER")
    country = _env("PROXY_COUNTRY")
    if country and user and "_country-" not in user.lower():
        return f"{user}_country-{country.lower()}"
    return user


def proxy_url() -> str | None:
    """URL completa para requests: http://user:pass@host:port"""
    if not proxy_habilitado():
        return None
    host = _env("PROXY_HOST")
    port = _env("PROXY_PORT")
    user = proxy_user()
    password = _env("PROXY_PASS")
    scheme = _env("PROXY_SCHEME") or "http"
    if user:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    else:
        auth = ""
    return f"{scheme}://{auth}{host}:{port}"


def proxy_para_2captcha() -> tuple[str | None, str | None]:
    """
    2Captcha resuelve el token saliendo por NUESTRO proxy (mismo IP residencial
    que hará el POST a SEACE). Formato: user:pass@host:port + proxytype=HTTP.
    """
    if not proxy_habilitado():
        return None, None
    user = proxy_user()
    password = _env("PROXY_PASS")
    host = _env("PROXY_HOST")
    port = _env("PROXY_PORT")
    ptype = (_env("PROXY_TYPE") or "HTTP").upper()
    if user:
        return f"{user}:{password}@{host}:{port}", ptype
    return f"{host}:{port}", ptype


def requests_proxies() -> dict[str, str] | None:
    url = proxy_url()
    if not url:
        return None
    return {"http": url, "https": url}


def aplicar_proxy(session) -> bool:
    """Inyecta el proxy en una requests.Session. Devuelve True si quedó activo."""
    proxies = requests_proxies()
    if not proxies:
        return False
    session.proxies.update(proxies)
    session.trust_env = False
    return True


def log_proxy_status(prefijo: str = "[+]") -> None:
    if not proxy_habilitado():
        print(f"{prefijo} Proxy IPRoyal: desactivado (tráfico directo)")
        return
    print(
        f"{prefijo} Proxy IPRoyal: {_env('PROXY_HOST')}:{_env('PROXY_PORT')} "
        f"country={_env('PROXY_COUNTRY') or 'dashboard'} user={proxy_user()[:16]}…"
    )
