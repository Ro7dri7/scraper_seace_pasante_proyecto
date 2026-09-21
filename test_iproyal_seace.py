"""
Prueba de fuego local: IPRoyal vs WAF de Oracle en SEACE PROD2.

Uso:
  python test_iproyal_seace.py

Lee PROXY_* del .env. Hace GET al buscador público. No gasta 2Captcha.
"""
from __future__ import annotations

import sys

import requests

from proxy_iproyal import aplicar_proxy, log_proxy_status, proxy_habilitado, proxy_url
from supabase_sync import load_env

load_env()

URL = (
    "https://prod2.seace.gob.pe/seacebus-uiwd-pub/"
    "buscadorPublico/buscadorPublico.xhtml"
)


def main() -> int:
    print("=" * 70)
    print(" TEST IPROYAL → SEACE PROD2 (WAF Oracle)")
    print("=" * 70)
    if not proxy_habilitado():
        print("❌ Faltan PROXY_HOST / PROXY_PORT (o PROXY_ENABLED=0).")
        print("   Completa PROXY_USER y PROXY_PASS de IPRoyal (Country: PE).")
        return 2

    # No imprimir user:pass. Solo el host.
    url = proxy_url() or ""
    host = url.split("@")[-1] if "@" in url else url
    print(f"[*] Proxy URL (sin secretos): http://***:***@{host}")
    log_proxy_status("[*]")

    s = requests.Session()
    aplicar_proxy(s)
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9",
    })

    try:
        r = s.get(URL, timeout=45)
    except requests.RequestException as e:
        print(f"❌ Error de red / proxy: {e}")
        return 1

    print(f"[*] Status code: {r.status_code}")
    if r.status_code == 200:
        print("✅ WAF Evadido")
        return 0
    if r.status_code == 403:
        print("❌ Proxy detectado o mal configurado")
        return 1
    print(f"❌ Respuesta inesperada HTTP {r.status_code}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
