"""
SEACE PROD2 (Licitaciones Mayores) — listado HTTP (PrimeFaces AJAX) + SQLite.

Modos:
  backfill → ventana fija (FECHA_INICIO/FIN), barra todas las páginas.
  sync     → ventana radar 48–72h. Ficha 2Captcha solo de nomenclaturas nuevas no bloqueadas.

reCAPTCHA: 2Captcha (TWOCAPTCHA_API_KEY). Las fichas se abren en la
misma página del listado, antes de paginar, para no invalidar el ViewState.

Estrategia on-demand: NO se descarga ningún PDF/ZIP. De cada documento de la
ficha se guarda el file_code y la URL pública de SeaceWeb-PRO
(SdescargarArchivoAlfresco), que abre la descarga directa.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
import warnings
from datetime import date, datetime, timedelta, timezone

import socket

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RequestException, Timeout
from urllib3.exceptions import ProtocolError, ReadTimeoutError

try:
    from urllib3.util.retry import Retry
except ImportError:  # urllib3 < 1.26
    from urllib3.util import Retry

from captcha_2captcha import (
    CaptchaError,
    CaptchaZeroBalance,
    extraer_action_v3,
    extraer_sitekey,
    resolver_recaptcha,
)
from lib_embudo import clasificar_nombre_etapa, horas_radar_default
from lib_nomenclatura import normalizar_nomenclatura
from proxy_iproyal import aplicar_proxy, log_proxy_status, proxy_para_2captcha
from supabase_sync import construir_url_descarga_prod2

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


def _env(name, default=""):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip()


def _env_int(name, default=None):
    raw = _env(name, "")
    if raw == "":
        return default
    return int(raw)


def _env_float(name, default):
    raw = _env(name, "")
    if raw == "":
        return default
    return float(raw)


def _env_bool(name, default=False):
    raw = _env(name, "")
    if raw == "":
        return default
    return raw.lower() in ("1", "true", "yes", "y", "si", "sí", "on")


FORM = "tbBuscador:idFormBuscarProceso"
DT = f"{FORM}:dtProcesos"
HOST = _env("SEACE_HOST", "https://prod2.seace.gob.pe")
ALFRESCO = _env("SEACE_ALFRESCO", "https://alfprod.seace.gob.pe/alfresco")
IP_CLIENTE = _env("SEACE_IP_CLIENTE", "")
TOKEN_FILE = Path(_env("SEACE_TOKEN_FILE") or Path(__file__).with_name("token.txt"))
DB_FILE = Path(_env("SEACE_DB_FILE") or Path(__file__).with_name("seace.db"))
NUEVAS_FILE = Path(_env("SEACE_NUEVAS_FILE") or Path(__file__).with_name("nuevas.json"))

MODO = _env("SEACE_MODO", "sync")
ANIO = _env("SEACE_YEAR", "2026")
FECHA_INICIO = _env("SEACE_FECHA_INICIO", "01/08/2026")
FECHA_FIN = _env("SEACE_FECHA_FIN", "31/08/2026")
VERSION_SEACE = _env("SEACE_VERSION", "Seace 3")
_OBJETO_RAW = _env("SEACE_OBJETO", "")
OBJETO = _OBJETO_RAW or None

DIAS_LOOKBACK = _env_int("SEACE_DIAS_LOOKBACK", 3)
HORAS_RADAR = _env_int("EMBUDO_HORAS_RADAR", horas_radar_default())
DIAS_SOLAPE = _env_int("SEACE_DIAS_SOLAPE", 1)

ROWS = _env_int("SEACE_ROWS", 20)
SLEEP_SEC = _env_float("SEACE_SLEEP_SEC", 1.2)
MAX_PAGES = _env_int("SEACE_MAX_PAGES", None)
# (connect, read): un socket colgado de Oracle no puede bloquear el pipeline.
HTTP_TIMEOUT = (15, 30)
HTTP_REINTENTOS = 3
HTTP_EXCEPCIONES = (
    RequestException,
    Timeout,
    RequestsConnectionError,
    ReadTimeoutError,
    ProtocolError,
    socket.timeout,
)
# POST tiene que reintentarse: SEACE pagina y abre fichas con ViewState vía POST.
_RETRY_METHODS = frozenset(
    {"HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"}
)
GUARDAR_XML_PAGINAS = _env_bool("SEACE_GUARDAR_XML", False)

PROCESAR_FICHAS_NUEVAS = _env_bool("SEACE_PROCESAR_FICHAS", True)
MAX_FICHAS_POR_CORRIDA = _env_int("SEACE_MAX_FICHAS", None)
FICHA_DIR = Path(_env("SEACE_FICHA_DIR") or Path(__file__).with_name("fichas"))
# Solo para depurar el parser; en producción no se escribe nada a disco.
GUARDAR_FICHA_HTML = _env_bool("SEACE_GUARDAR_FICHA_HTML", False)
_handoff_raw = _env("OECE_HANDOFF_FILE", "")
if _handoff_raw:
    _handoff_path = Path(_handoff_raw)
    if not _handoff_path.is_absolute():
        _handoff_path = Path(__file__).resolve().parent / _handoff_path
    HANDOFF_DEFAULT = str(_handoff_path)
else:
    HANDOFF_DEFAULT = str(Path(__file__).with_name("handoff_state.json"))


def _retry_urllib3():
    """Retry compatible con urllib3 nuevo (allowed_methods) y viejo (method_whitelist)."""
    kwargs = {
        "total": 5,
        "connect": 5,
        "read": 5,
        "backoff_factor": 2,
        "status_forcelist": [500, 502, 503, 504],
        "raise_on_status": False,
    }
    try:
        return Retry(allowed_methods=_RETRY_METHODS, **kwargs)
    except TypeError:
        return Retry(method_whitelist=_RETRY_METHODS, **kwargs)


def _montar_adaptador_reintentos(session):
    adapter = HTTPAdapter(max_retries=_retry_urllib3())
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# Cliente SEACE
# ---------------------------------------------------------------------------
class Seace:
    def __init__(self, host=HOST, calentar=True):
        self.host = host
        self.page = f"{host}/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml"
        self.s = requests.Session()
        _montar_adaptador_reintentos(self.s)
        self.s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9",
        })
        if aplicar_proxy(self.s):
            log_proxy_status("[+] PROD2")
        self._vs = None
        self.filtros = {}
        self.sitekey = None
        if calentar:
            self.refresh()

    def sesion_caliente(self):
        """True si ya hay JSESSIONID + ViewState del buscador."""
        return bool(self._vs) and bool(self.s.cookies.get("JSESSIONID"))

    def _log_sesion(self, prefijo=""):
        jsid = self.s.cookies.get("JSESSIONID") or ""
        vs = (self._vs or "")[:20]
        print(
            f"{prefijo}sesión JSESSIONID="
            f"{(jsid[:10] + '…') if jsid else 'AUSENTE'} "
            f"ViewState={vs + '…' if vs else 'AUSENTE'}"
        )

    def _http(self, method, url, **kwargs):
        """GET/POST: timeout obligatorio + reintentos. No traga KeyboardInterrupt."""
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = HTTP_TIMEOUT
        ultimo = None
        for intento in range(1, HTTP_REINTENTOS + 1):
            try:
                return self.s.request(method, url, **kwargs)
            except (Timeout, RequestsConnectionError) as e:
                ultimo = e
                print(
                    "[!] Timeout en SEACE, reintentando o saltando... "
                    f"({method} {intento}/{HTTP_REINTENTOS}: {e})"
                )
            except HTTP_EXCEPCIONES as e:
                ultimo = e
                print(
                    f"  [!] HTTP {method} intento {intento}/{HTTP_REINTENTOS}: {e}"
                )
            if intento >= HTTP_REINTENTOS:
                break
            time.sleep(min(8.0, 1.5 ** intento))
        raise ultimo or RuntimeError(f"HTTP {method} falló sin excepción capturada")

    def refresh(self):
        r = self._http("GET", self.page)
        if r.status_code == 403:
            raise RuntimeError(
                "WAF 403 en PROD2. El proxy IPRoyal no está activo, "
                "está mal autenticado o la IP residencial fue marcada."
            )
        r.raise_for_status()
        self.soup = BeautifulSoup(r.text, "lxml")
        vs = self.soup.find("input", {"name": "javax.faces.ViewState"})
        if not vs:
            Path("error_get.html").write_text(r.text, encoding="utf-8")
            raise ValueError("No se pudo obtener el ViewState inicial.")
        self._vs = vs["value"]
        self.page_html = r.text
        self.sitekey = extraer_sitekey(r.text) or _env("SEACE_RECAPTCHA_SITEKEY")
        print(f"[+] ViewState: {self._vs[:20]}...")
        if self.sitekey:
            print(f"[+] reCAPTCHA v3 sitekey: {self.sitekey[:16]}...")
        self._log_sesion("[+] Buscador caliente — ")

    def find_select(self, option_label):
        form = self.soup.find("form", id=FORM)
        for sel in form.select("select[name]"):
            for opt in sel.find_all("option"):
                if opt.get_text(strip=True) == option_label:
                    return sel["name"], opt.get("value", "")
        return None, None

    def _absorb_viewstate(self, xml_text):
        patterns = [
            r'<update id="[^"]*javax\.faces\.ViewState[^"]*"><!\[CDATA\[(.*?)\]\]>',
            r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"',
        ]
        for pat in patterns:
            ms = re.findall(pat, xml_text, re.S)
            if ms:
                self._vs = ms[-1].strip()
                return True
        return False

    def _headers_ajax(self):
        return {
            "Faces-Request": "partial/ajax",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": self.page,
            "Origin": self.host,
            "Accept": "application/xml, text/xml, */*; q=0.01",
        }

    def _merge_form_fields(self, data):
        form = self.soup.find("form", id=FORM)
        if not form:
            return data
        for inp in form.select("input[name]"):
            n = inp["name"]
            if n not in data:
                data[n] = inp.get("value", "")
        for sel in form.select("select[name]"):
            n = sel["name"]
            if n not in data:
                chosen = sel.find("option", selected=True) or sel.find("option")
                data[n] = chosen.get("value", "") if chosen else ""
        return data

    def ajax(self, source, execute, render, extra=None, event=None, partial_event=None):
        data = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": source,
            "javax.faces.partial.execute": execute,
            "javax.faces.partial.render": render,
            FORM: FORM,
            "javax.faces.ViewState": self._vs,
        }
        if event:
            data["javax.faces.behavior.event"] = event
        if partial_event:
            data["javax.faces.partial.event"] = partial_event
        data = self._merge_form_fields(data)
        if extra:
            data.update(extra)
        r = self._http("POST", self.page, data=data, headers=self._headers_ajax())
        r.raise_for_status()
        self._absorb_viewstate(r.text)
        return r.text

    def buscar(self, token, anio, fecha_ini, fecha_fin, version_label, objeto_label):
        ver_name, ver_val = self.find_select(version_label)
        obj_name = obj_val = None
        if objeto_label:
            obj_name, obj_val = self.find_select(objeto_label)
            if not obj_name:
                raise ValueError(f"Objeto '{objeto_label}' no encontrado")

        expand = {
            inp["name"]: "false"
            for inp in self.soup.find("form", id=FORM).select('input[name$="_collapsed"]')
        }
        source = f"{FORM}:btnBuscarSel"
        self.filtros = {
            FORM: FORM,
            f"{FORM}:numPositionTabView": "1",
            f"{FORM}:anioConvocatoria_input": anio,
            f"{FORM}:dfechaInicio_input": fecha_ini,
            f"{FORM}:dfechaFin_input": fecha_fin,
            f"{FORM}:tokenBusProSel": token,
            f"{FORM}:ipClienteIpify": IP_CLIENTE,
            "g-recaptcha-response": token,
            "g-recaptcha-response-v3": token,
            **expand,
        }
        if ver_name:
            self.filtros[ver_name] = ver_val
        if obj_name:
            self.filtros[obj_name] = obj_val

        print(f"[*] buscar año={anio} objeto={objeto_label!r} {fecha_ini}..{fecha_fin}")
        xml = self.ajax(
            source=source,
            execute="@all",
            render=(
                f"{FORM}:pnlGrdResultadosProcesos {FORM}:footerBuscador "
                f"frmMesajes:gPrincipal {FORM}:btnBuscarSel {FORM}:pnlBuscarProceso"
            ),
            extra={**self.filtros, source: source, "submit": "S"},
        )
        print(f"[+] ViewState post-buscar: {self._vs[:24]}...")
        return xml

    def paginar(self, first, rows=ROWS):
        data = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": DT,
            "javax.faces.partial.execute": DT,
            "javax.faces.partial.render": DT,
            "javax.faces.behavior.event": "page",
            "javax.faces.partial.event": "page",
            f"{DT}_pagination": "true",
            f"{DT}_first": str(first),
            f"{DT}_rows": str(rows),
            f"{DT}_encodeFeature": "true",
            FORM: FORM,
            "javax.faces.ViewState": self._vs,
            **self.filtros,
        }
        data = self._merge_form_fields(data)
        # filtros / pagination ganan
        data.update(self.filtros)
        data[f"{DT}_pagination"] = "true"
        data[f"{DT}_first"] = str(first)
        data[f"{DT}_rows"] = str(rows)
        data[f"{DT}_encodeFeature"] = "true"
        data["javax.faces.ViewState"] = self._vs

        try:
            r = self._http("POST", self.page, data=data, headers=self._headers_ajax())
            r.raise_for_status()
        except (Timeout, RequestsConnectionError) as e:
            print(
                f"[!] Timeout en SEACE, reintentando o saltando... "
                f"(paginar first={first}: {e})"
            )
            raise
        self._absorb_viewstate(r.text)
        return r.text

    def abrir_ficha(self, fila):
        """
        POST full-form (NO ajax) → 302 → fichaSeleccion.xhtml?id=UUID
        Calibrado con captura DevTools (Ficha de Selección).
        """
        source = fila.get("ficha_source")
        if not source or not fila.get("nid_convocatoria") or not fila.get("nid_proceso"):
            raise ValueError("fila sin ficha_source / nid_convocatoria / nid_proceso")

        data = self._merge_form_fields({
            FORM: FORM,
            "javax.faces.ViewState": self._vs,
            **self.filtros,
        })
        data.update({
            FORM: FORM,
            "javax.faces.ViewState": self._vs,
            source: source,
            "ntipo": fila.get("ntipo") or "1",
            "nidConvocatoria": fila["nid_convocatoria"],
            "nidProceso": str(fila["nid_proceso"]),
            "nidSistema": str(fila.get("nid_sistema") or "3"),
            "ptoRetorno": fila.get("pto_retorno") or "LOCAL",
        })
        # re-aplicar filtros de búsqueda (token, fechas, etc.)
        data.update(self.filtros)
        data["javax.faces.ViewState"] = self._vs
        data[source] = source

        headers = {
            "User-Agent": self.s.headers.get("User-Agent"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.host,
            "Referer": self.page,
            "Upgrade-Insecure-Requests": "1",
        }
        r = self._http(
            "POST", self.page, data=data, headers=headers, allow_redirects=False,
        )
        ficha_markers = (
            "tbFicha:dtCronograma",
            "tbFicha:dtDocumentos",
            "fichaSeleccion",
            "dtDocumentos_data",
        )
        if r.status_code == 200 and any(m in (r.text or "") for m in ficha_markers):
            _validar_html_ficha(r.status_code, r.text, r.url or self.page)
            return r.url or self.page, r.text

        if r.status_code not in (301, 302, 303, 307, 308):
            Path("ficha_post_error.html").write_text(r.text or "", encoding="utf-8")
            raise RuntimeError(
                f"abrir_ficha esperaba 302, got {r.status_code} (ver ficha_post_error.html)"
            )

        loc = r.headers.get("Location") or ""
        if loc.startswith("http://"):
            loc = "https://" + loc[len("http://"):]
        elif loc.startswith("/"):
            loc = self.host + loc
        if "fichaSeleccion.xhtml" not in loc:
            print(f"[!] Redirect 302 a URL inesperada (no fichaSeleccion): {loc}")
            raise RuntimeError(f"Redirect inesperado: {loc}")

        # Misma Session (JSESSIONID). allow_redirects=False para detectar 302 extras.
        rf = self._http(
            "GET",
            loc,
            headers={
                "User-Agent": self.s.headers.get("User-Agent"),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": self.page,
            },
            allow_redirects=False,
        )
        if rf.status_code in (301, 302, 303, 307, 308):
            loc2 = rf.headers.get("Location") or ""
            print(f"[!] GET ficha devolvió {rf.status_code} → {loc2}; se sigue la Location")
            if loc2.startswith("/"):
                loc2 = self.host + loc2
            rf = self._http(
                "GET",
                loc2,
                headers={
                    "User-Agent": self.s.headers.get("User-Agent"),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": loc,
                },
            )
            loc = loc2
        if rf.status_code != 200:
            print(f"[!] GET fichaSeleccion HTTP {rf.status_code} (se esperaba 200) {loc}")
        rf.raise_for_status()
        _validar_html_ficha(rf.status_code, rf.text, loc)
        return loc, rf.text

    def get_ficha_url(self, url):
        """GET fichaSeleccion con la Session ya calentada (JSESSIONID + ViewState)."""
        if not self.sesion_caliente():
            print("[*] Sesión fría: GET buscadorPublico.xhtml (cookies + ViewState)")
            self.refresh()
        if not self.sesion_caliente():
            raise RuntimeError(
                "No se pudo calentar la sesión JSF (falta JSESSIONID o ViewState)."
            )
        self._log_sesion("[*] GET ficha con ")
        r = self._http(
            "GET",
            url,
            headers={
                "User-Agent": self.s.headers.get("User-Agent"),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": self.page,
            },
            allow_redirects=False,
        )
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location") or ""
            loc_abs = loc if loc.startswith("http") else (
                self.host + loc if loc.startswith("/") else loc
            )
            print(f"[!] GET ficha {r.status_code} → {loc_abs}")
            if "buscadorPublico" in loc_abs:
                raise RuntimeError(
                    "Redirección detectada o Ficha no encontrada. "
                    f"HTML descargado: el servidor mandó a {loc_abs}"
                )
            r = self._http(
                "GET",
                loc_abs,
                headers={
                    "User-Agent": self.s.headers.get("User-Agent"),
                    "Referer": url,
                },
                allow_redirects=False,
            )
            url = loc_abs
            if r.status_code in (301, 302, 303, 307, 308):
                loc2 = r.headers.get("Location") or ""
                raise RuntimeError(
                    "Redirección detectada o Ficha no encontrada. "
                    f"HTML descargado: segundo 302 → {loc2}"
                )
        r.raise_for_status()
        if "buscadorPublico" in (r.url or url) and "fichaSeleccion" not in (r.url or url):
            titulo = _titulo_html(r.text)
            raise RuntimeError(
                "Redirección detectada o Ficha no encontrada. "
                f"HTML descargado: {titulo}"
            )
        _validar_html_ficha(r.status_code, r.text, url)
        return url, r.text

    def descargar_archivo(self, url, dest=None):
        """GET de Alfresco/SEACE con el mismo timeout y reintentos. No se usa
        en el embudo (on-demand); queda para no colgar si se llama a mano."""
        r = self._http("GET", url, stream=True)
        r.raise_for_status()
        if dest:
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
            return dest
        return r.content

# ---------------------------------------------------------------------------
# Parseo
# ---------------------------------------------------------------------------
def detectar_ip_publica():
    global IP_CLIENTE
    if IP_CLIENTE:
        print(f"[+] IP pública (SEACE_IP_CLIENTE): {IP_CLIENTE}")
        return IP_CLIENTE
    try:
        s = requests.Session()
        aplicar_proxy(s)
        ip = s.get("https://api.ipify.org", timeout=15).text.strip()
        if ip:
            IP_CLIENTE = ip
            print(f"[+] IP pública (vía proxy si está activo): {IP_CLIENTE}")
    except Exception as e:
        print(f"[!] No se pudo detectar IP pública ({e}); se usa {IP_CLIENTE or 'vacío'}")
    return IP_CLIENTE


def obtener_token_recaptcha(seace):
    html = getattr(seace, "page_html", "") or ""
    sitekey = extraer_sitekey(html) or seace.sitekey or _env("SEACE_RECAPTCHA_SITEKEY")
    if not sitekey:
        raise SystemExit(
            "[-] No se encontró sitekey de reCAPTCHA en el HTML. "
            "Revisa SEACE_RECAPTCHA_SITEKEY en .env"
        )
    print(f"[+] Sitekey extraído del HTML: {sitekey[:16]}...")
    action = _env("SEACE_RECAPTCHA_ACTION") or extraer_action_v3(html)
    proxy, proxytype = proxy_para_2captcha()
    try:
        token = resolver_recaptcha(
            sitekey,
            seace.page,
            version=_env("SEACE_RECAPTCHA_VERSION", "v3"),
            action=action,
            min_score=float(_env("SEACE_RECAPTCHA_MIN_SCORE", "0.3") or 0.3),
            proxy=proxy,
            proxytype=proxytype or "HTTP",
        )
    except CaptchaZeroBalance as e:
        raise SystemExit(f"[-] {e}") from e
    except CaptchaError as e:
        raise SystemExit(f"[-] Error 2Captcha: {e}") from e
    if len(token) < 100:
        raise SystemExit("[-] Token 2Captcha incompleto")
    print(f"[+] Token reCAPTCHA listo (len={len(token)})")
    return token


def cargar_nomenclaturas_oece(path):
    known = set()
    if not path:
        return known
    p = Path(path)
    if not p.exists():
        print(f"[!] Handoff OECE no existe: {p}")
        return known
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("nomenclaturas_norm") or data.get("nomenclaturas") or []
        if isinstance(items, dict):
            items = list(items.keys())
        for item in items:
            n = normalizar_nomenclatura(item)
            if n:
                known.add(n)
    else:
        for line in p.read_text(encoding="utf-8").splitlines():
            n = normalizar_nomenclatura(line)
            if n:
                known.add(n)
    print(f"[+] Nomenclaturas OECE cargadas para deduplicar: {len(known)}")
    try:
        from supabase_sync import nomenclaturas_bloqueadas, nomenclaturas_en_nube
        cloud = nomenclaturas_en_nube()
        bloqueadas = nomenclaturas_bloqueadas()
        known |= cloud | bloqueadas
        print(f"[+] Nomenclaturas en Supabase: {len(cloud)} (unión={len(known)})")
        print(f"[+] Bloqueadas por OECE (se omiten fichas): {len(bloqueadas)}")
    except Exception as e:
        print(f"[!] No se pudieron leer nomenclaturas de Supabase: {e}")
    return known


def fecha_iso_a_seace(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return s


def _fragmento_grilla(xml_response):
    for uid in (DT, f"{FORM}:pnlGrdResultadosProcesos"):
        m = re.search(
            rf'<update id="{re.escape(uid)}"><!\[CDATA\[(.*?)\]\]></update>',
            xml_response, re.S,
        )
        if m:
            return m.group(1), uid
    for m in re.finditer(
        r'<update id="([^"]+)"><!\[CDATA\[(.*?)\]\]></update>', xml_response, re.S
    ):
        if "dtProcesos_data" in m.group(2) or "ui-datatable" in m.group(2):
            return m.group(2), m.group(1)
    return None, None


def _parse_ficha_link(acciones_td):
    """
    En Acciones hay varios links. La Ficha lleva ptoRetorno=LOCAL (+ ntipo).
    Historial solo trae nidConvocatoria/nidProceso/nidSistema.
    """
    for a in acciones_td.find_all("a"):
        onclick = a.get("onclick") or ""
        if "ptoRetorno" not in onclick and "'ntipo'" not in onclick:
            continue
        src = a.get("id") or a.get("name")
        if not src:
            continue
        params = dict(re.findall(r"'([^']+)':'([^']*)'", onclick))
        # addSubmitParam mete también el propio source como key
        return {
            "ficha_source": src,
            "nid_convocatoria": params.get("nidConvocatoria"),
            "nid_proceso": params.get("nidProceso"),
            "nid_sistema": params.get("nidSistema", "3"),
            "ntipo": params.get("ntipo", "1"),
            "pto_retorno": params.get("ptoRetorno", "LOCAL"),
        }
    # fallback: cualquier addSubmitParam con nidConvocatoria
    for a in acciones_td.find_all("a"):
        onclick = a.get("onclick") or ""
        if "nidConvocatoria" not in onclick or "addSubmitParam" not in onclick:
            continue
        src = a.get("id") or a.get("name")
        params = dict(re.findall(r"'([^']+)':'([^']*)'", onclick))
        return {
            "ficha_source": src,
            "nid_convocatoria": params.get("nidConvocatoria"),
            "nid_proceso": params.get("nidProceso"),
            "nid_sistema": params.get("nidSistema", "3"),
            "ntipo": params.get("ntipo", "1"),
            "pto_retorno": params.get("ptoRetorno", "LOCAL"),
        }
    return {}


def parse_resultados(xml_response):
    html, _uid = _fragmento_grilla(xml_response)
    rc = re.search(r"rowCount:(\d+)", xml_response)
    total = int(rc.group(1)) if rc else 0
    if not html:
        return [], total

    soup = BeautifulSoup(html, "lxml")
    tbody = soup.find("tbody", id=f"{DT}_data") or soup.find(
        "tbody", id=re.compile(r"dtProcesos_data")
    )
    if tbody:
        trs = tbody.find_all("tr", recursive=False)
    else:
        trs = soup.find_all("tr", recursive=True)
        data_trs = [
            tr for tr in trs
            if tr.get("data-ri") is not None
            or any(
                c.startswith("ui-datatable-") and c != "ui-datatable-empty-message"
                for c in (tr.get("class") or [])
            )
        ]
        if data_trs:
            trs = data_trs

    rows = []
    for tr in trs:
        if "ui-datatable-empty-message" in (tr.get("class") or []):
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 12:
            continue
        ficha = _parse_ficha_link(tds[-1])
        rows.append({
            "n": tds[0].get_text(" ", strip=True),
            "entidad": tds[1].get_text(" ", strip=True),
            "fecha_publicacion": tds[2].get_text(" ", strip=True),
            "nomenclatura": tds[3].get_text(" ", strip=True),
            "nomenclatura_norm": normalizar_nomenclatura(tds[3].get_text(" ", strip=True)),
            "reiniciado_desde": tds[4].get_text(" ", strip=True),
            "objeto": tds[5].get_text(" ", strip=True),
            "descripcion": tds[6].get_text(" ", strip=True),
            "vr_ve_cuantia": tds[9].get_text(" ", strip=True),
            "moneda": tds[10].get_text(" ", strip=True),
            "version_seace": tds[11].get_text(" ", strip=True),
            "nid_convocatoria": ficha.get("nid_convocatoria"),
            "nid_proceso": ficha.get("nid_proceso"),
            "nid_sistema": ficha.get("nid_sistema"),
            "ntipo": ficha.get("ntipo"),
            "pto_retorno": ficha.get("pto_retorno"),
            "ficha_source": ficha.get("ficha_source"),
        })
    return rows, total


def _absolutizar_url(href):
    href = (href or "").strip()
    if not href or href.startswith("javascript") or href in ("#", "void(0)"):
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return HOST.rstrip("/") + href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return ""


def parse_estado_ficha(ficha_html):
    """Estado del proceso en la ficha JSF (nunca se asume vacío aguas abajo)."""
    soup = BeautifulSoup(ficha_html, "lxml")
    for lab in soup.find_all(["label", "span", "td", "th"]):
        txt = lab.get_text(" ", strip=True).lower()
        if txt not in ("estado", "estado del proceso", "estado proceso") and not (
            txt.startswith("estado") and len(txt) < 28
        ):
            continue
        nxt = lab.find_next(["span", "td", "div", "input"])
        if nxt is None:
            continue
        if nxt.name == "input":
            val = (nxt.get("value") or "").strip()
        else:
            val = nxt.get_text(" ", strip=True)
        if val and val.lower() not in ("estado", "estado del proceso"):
            return val
    m = re.search(
        r"Estado(?:\s+del\s+proceso)?\s*[:\-]\s*([A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]{3,40})",
        ficha_html,
        re.I,
    )
    return (m.group(1).strip() if m else "") or ""


CRONO_MARKER = "tbFicha:dtCronograma"
FICHA_DRY_RUN_URL = (
    "https://prod2.seace.gob.pe/seacebus-uiwd-pub/fichaSeleccion/"
    "fichaSeleccion.xhtml?id=eef2f265-c214-4511-bdb1-678827e4d090&ptoRetorno=LOCAL"
)


def _validar_html_ficha(status, html, url=""):
    html = html or ""
    ok = True
    if status != 200:
        print(f"[!] Ficha HTTP {status} (se esperaba 200) {url}")
        ok = False
    if CRONO_MARKER not in html:
        print(
            f"[!] HTML de ficha incompleto: no contiene '{CRONO_MARKER}' {url}"
        )
        ok = False
    return ok


def _nombre_etapa_celda(td):
    """Primer nodo de texto de la celda (ignora <br> y <span> secundarios)."""
    for node in td.contents:
        if isinstance(node, str):
            t = " ".join(node.split())
            if t:
                return t
        if getattr(node, "name", None) == "br":
            break
    linea = td.get_text("\n", strip=True).split("\n")[0]
    return " ".join(linea.split())


def _parse_fecha_cronograma(texto):
    raw = " ".join((texto or "").split())
    m = re.search(r"(\d{2}/\d{2}/\d{4}(?:\s+\d{1,2}:\d{2})?)", raw)
    if not m:
        return None, None
    s = m.group(1)
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt, dt.isoformat()
        except ValueError:
            continue
    return None, None


def _clave_etapa_prod2(nombre):
    n = (nombre or "").lower()
    n = re.sub(r"\s+", " ", n)
    if "buena pro" in n or (n.startswith("otorg") and "pro" in n):
        return "otorgamiento_buena_pro"
    if "integrac" in n:
        return "integracion_bases"
    if "present" in n and "propuest" in n:
        return "presentacion_propuestas"
    if "calific" in n or "evaluac" in n:
        return "calificacion_evaluacion"
    if "absoluc" in n:
        return "absolucion_consultas"
    if "formulac" in n or ("consulta" in n and "observ" in n):
        return "formulacion_consultas"
    if "registro" in n and "particip" in n:
        return "registro_participantes"
    if n.startswith("convocatoria"):
        return "convocatoria"
    return clasificar_nombre_etapa(nombre) or "otra"


def inferir_etapa_actual(etapas, ahora=None):
    ahora = ahora or datetime.now()
    vigente = None
    ultima = None
    for et in etapas:
        ini, fin = et.get("fecha_inicio_dt"), et.get("fecha_fin_dt")
        if ini and fin:
            if ini <= ahora <= fin:
                vigente = et["nombre"]
                break
            if ahora > fin:
                ultima = et["nombre"]
        elif ini and fin is None and ahora >= ini:
            vigente = et["nombre"]
        elif fin and ini is None and ahora <= fin:
            vigente = et["nombre"]
    if vigente:
        return vigente
    if ultima:
        return ultima
    if etapas:
        return etapas[0]["nombre"]
    return "Sin cronograma"


def _titulo_html(html_doc):
    soup = BeautifulSoup(html_doc or "", "lxml")
    tag = soup.find("title")
    if tag and tag.get_text(strip=True):
        return tag.get_text(" ", strip=True)
    h1 = soup.find("h1")
    return (h1.get_text(" ", strip=True) if h1 else "") or "(sin título)"


def extraer_cronograma_prod2(html_doc):
    """
    Parser del tbody#tbFicha:dtCronograma_data (3 celdas: etapa, inicio, fin).
    Devuelve etapas (8 hitos) + columnas listas para convocatorias.
    """
    html_doc = html_doc or ""
    if "tbFicha:dtCronograma_data" not in html_doc:
        titulo = _titulo_html(html_doc)
        msg = (
            "Redirección detectada o Ficha no encontrada. "
            f"HTML descargado: {titulo}"
        )
        print(f"[!] Error crítico: {msg}")
        raise ValueError(msg)
    soup = BeautifulSoup(html_doc, "lxml")
    tbody = soup.find("tbody", id="tbFicha:dtCronograma_data")
    if not tbody:
        tbody = soup.find("tbody", id=re.compile(r"dtCronograma_data"))
    etapas = []
    if tbody:
        for tr in tbody.find_all("tr"):
            if "ui-datatable-empty-message" in (tr.get("class") or []):
                continue
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 3:
                tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            nombre = _nombre_etapa_celda(tds[0])
            if not nombre:
                continue
            ini_dt, ini_iso = _parse_fecha_cronograma(tds[1].get_text(" ", strip=True))
            fin_dt, fin_iso = _parse_fecha_cronograma(tds[2].get_text(" ", strip=True))
            etapas.append({
                "clave": _clave_etapa_prod2(nombre),
                "nombre": nombre,
                "fecha_inicio": ini_iso,
                "fecha_fin": fin_iso,
                "fecha_inicio_dt": ini_dt,
                "fecha_fin_dt": fin_dt,
            })

    por = {et["clave"]: et for et in etapas}
    conv = por.get("convocatoria") or {}
    form = por.get("formulacion_consultas") or {}
    abso = por.get("absolucion_consultas") or {}
    inte = por.get("integracion_bases") or {}
    pres = por.get("presentacion_propuestas") or {}
    otor = por.get("otorgamiento_buena_pro") or {}

    mapeo = {
        "fecha_publicacion": conv.get("fecha_inicio") or conv.get("fecha_fin"),
        "fecha_inicio_consultas": form.get("fecha_inicio"),
        "fecha_fin_consultas": abso.get("fecha_fin") or abso.get("fecha_inicio"),
        "fecha_integracion": inte.get("fecha_fin") or inte.get("fecha_inicio"),
        "fecha_presentacion": pres.get("fecha_inicio") or pres.get("fecha_fin"),
        "fecha_fin_cotizacion": otor.get("fecha_fin") or otor.get("fecha_inicio"),
        "etapa_actual": inferir_etapa_actual(etapas),
    }
    return {
        "etapas": [
            {
                "clave": et["clave"],
                "nombre": et["nombre"],
                "fecha_inicio": et["fecha_inicio"],
                "fecha_fin": et["fecha_fin"],
            }
            for et in etapas
        ],
        "mapeo_convocatorias": mapeo,
        **mapeo,
    }


def parse_cronograma_ficha(ficha_html):
    """Compat: solo las columnas que upsert_seace_fila escribe en convocatorias."""
    data = extraer_cronograma_prod2(ficha_html)
    return {
        "fecha_publicacion": data.get("fecha_publicacion"),
        "fecha_inicio_consultas": data.get("fecha_inicio_consultas"),
        "fecha_fin_consultas": data.get("fecha_fin_consultas"),
        "fecha_inicio_cotizacion": None,
        "fecha_fin_cotizacion": data.get("fecha_fin_cotizacion"),
        "fecha_integracion": data.get("fecha_integracion"),
        "fecha_presentacion": data.get("fecha_presentacion"),
        "etapa_actual": data.get("etapa_actual"),
    }


def parse_documentos(ficha_html):
    """
    Extrae docs de dtDocumentos.
    Preferimos href directo o descargaDocGeneral(uuid, tipo, nombre).
    Si no hay URL usable, se arma el servlet prod1 (SdescargarArchivoAlfresco),
    no el resolver Alfresco downloadDoc.
    """
    soup = BeautifulSoup(ficha_html, "lxml")
    tbody = soup.find("tbody", id="tbFicha:dtDocumentos_data")
    if not tbody:
        tbody = soup.find("tbody", id=re.compile(r"dtDocumentos_data"))
    docs = []
    if not tbody:
        for uuid, tipo, nombre in re.findall(
            r"descargaDocGeneral\('([^']*)','([^']*)','([^']*)'\)", ficha_html
        ):
            docs.append({
                "n": None, "etapa": None, "documento": None,
                "file_id": uuid, "tipo": tipo, "nombre_archivo": nombre,
                "fuente": "js",
                "url_descarga": "",
                "categoria": clasificar_documento(None, None, nombre),
            })
        return docs

    for tr in tbody.find_all("tr", recursive=False):
        if "ui-datatable-empty-message" in (tr.get("class") or []):
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue
        file_id = tipo = nombre = source = file_code = None
        url_descarga = ""
        for a in tr.find_all("a"):
            oc = a.get("onclick") or ""
            href = a.get("href") or ""
            url_descarga = url_descarga or _absolutizar_url(href)
            m = re.search(
                r"descargaDocGeneral\('([^']*)','([^']*)','([^']*)'\)", oc
            )
            if m:
                file_id, tipo, nombre = m.group(1), m.group(2), m.group(3)
                source = a.get("id")
            mcode = re.search(r"fileCode=([A-Za-z0-9_\-]+)", oc + " " + href)
            if mcode:
                file_code = mcode.group(1)
            if url_descarga or m:
                break
        if not file_id and file_code:
            file_id = file_code
        if not file_id and url_descarga:
            file_id = url_descarga
        if not file_id:
            continue
        etapa = tds[1].get_text(" ", strip=True)
        documento = tds[2].get_text(" ", strip=True)
        docs.append({
            "n": tds[0].get_text(" ", strip=True),
            "etapa": etapa,
            "documento": documento,
            "file_id": file_id,
            "file_code": file_code or (file_id if file_id != url_descarga else ""),
            "tipo": tipo,
            "nombre_archivo": nombre,
            "fuente": source,
            "url_descarga": url_descarga,
            "fecha": tds[4].get_text(" ", strip=True) if len(tds) > 4 else None,
            "categoria": clasificar_documento(etapa, documento, nombre),
        })
    return docs


EXCLUSIONES_DOC = (
    "acta de absolucion", "acta de absolución", "absolucion", "absolución",
    "postergacion", "postergación", "constancia",
    "acta de evaluacion", "acta de evaluación", "buena pro",
    "subsanacion", "subsanación", "presentacion de propuesta",
    "presentación de propuesta",
)


def clasificar_documento(etapa, documento, nombre_archivo):
    """Clasifica doc publicado en ficha (puede no existir aún si el proceso sigue abierto)."""
    blob = " ".join(
        x.lower() for x in (etapa or "", documento or "", nombre_archivo or "")
    )
    if any(k in blob for k in EXCLUSIONES_DOC):
        return "excluido"
    if "bases integradas" in blob:
        return "bases_integradas"
    if "bases administrativas" in blob or (
        "base" in blob and ("administrativ" in blob or "estandar" in blob or "estándar" in blob)
    ):
        return "bases_administrativas"
    if "resumen ejecutivo" in blob:
        return "resumen_ejecutivo"
    if any(k in blob for k in (
        "documentos de presentacion", "documentos de presentación",
        "presentacion_de_propuestas", "presentación de ofertas",
        "presentacion de ofertas",
    )):
        return "presentacion_propuestas"
    if blob.strip().startswith("convocatoria") and "base" in blob:
        return "bases_administrativas"
    if "base" in blob:
        return "bases_administrativas"
    return "otro"


def elegir_documento_prioridad(docs):
    """Bases Integradas > Bases Administrativas > Resumen Ejecutivo. Ignora actas."""
    prioridad = {
        "bases_integradas": 100,
        "bases_administrativas": 50,
        "resumen_ejecutivo": 20,
    }
    mejor = None
    mejor_score = 0
    for d in docs:
        cat = d.get("categoria") or clasificar_documento(
            d.get("etapa"), d.get("documento"), d.get("nombre_archivo")
        )
        d["categoria"] = cat
        score = prioridad.get(cat, 0)
        if score > mejor_score:
            mejor, mejor_score = d, score
    return mejor


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS licitaciones (
            nid_convocatoria TEXT PRIMARY KEY,
            nomenclatura TEXT,
            entidad TEXT,
            fecha_publicacion TEXT,
            objeto TEXT,
            descripcion TEXT,
            vr_ve_cuantia TEXT,
            moneda TEXT,
            version_seace TEXT,
            nid_proceso TEXT,
            nid_sistema TEXT,
            ficha_url TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """
    )
    # migraciones suaves si la DB ya existía
    cols = {r[1] for r in conn.execute("PRAGMA table_info(licitaciones)")}
    for col, typ in (
        ("nid_proceso", "TEXT"),
        ("nid_sistema", "TEXT"),
        ("ficha_url", "TEXT"),
        ("docs_bases_ok", "INTEGER DEFAULT 0"),
        ("docs_propuestas_ok", "INTEGER DEFAULT 0"),
        ("docs_propuestas_pendiente", "INTEGER DEFAULT 1"),
        ("proxima_revision", "TEXT"),
        ("nomenclatura_norm", "TEXT"),
        ("fuente", "TEXT"),
        ("url_bases", "TEXT"),
        ("file_code", "TEXT"),
        ("bloqueada", "INTEGER DEFAULT 0"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE licitaciones ADD COLUMN {col} {typ}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documentos (
            file_id TEXT PRIMARY KEY,
            nid_convocatoria TEXT,
            categoria TEXT,
            etapa TEXT,
            documento TEXT,
            nombre_archivo TEXT,
            file_code TEXT,
            url_descarga TEXT,
            registrado_at TEXT,
            FOREIGN KEY(nid_convocatoria) REFERENCES licitaciones(nid_convocatoria)
        )
        """
    )
    cols_docs = {r[1] for r in conn.execute("PRAGMA table_info(documentos)")}
    for col, typ in (
        ("file_code", "TEXT"),
        ("url_descarga", "TEXT"),
        ("registrado_at", "TEXT"),
    ):
        if col not in cols_docs:
            conn.execute(f"ALTER TABLE documentos ADD COLUMN {col} {typ}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lic_nom_norm ON licitaciones(nomenclatura_norm)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lic_fecha ON licitaciones(fecha_publicacion)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lic_prop_pend "
        "ON licitaciones(docs_propuestas_pendiente)"
    )
    conn.commit()
    return conn


def db_count(conn):
    return conn.execute("SELECT COUNT(*) FROM licitaciones").fetchone()[0]


def parse_fecha_pub(s):
    """'31/08/2026 23:01' → date | None"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:16] if len(s) >= 16 and fmt.endswith("%M") else s[:10], fmt).date()
        except ValueError:
            continue
    return None


def db_max_fecha(conn):
    rows = conn.execute(
        "SELECT fecha_publicacion FROM licitaciones WHERE fecha_publicacion IS NOT NULL"
    ).fetchall()
    best = None
    for (fp,) in rows:
        d = parse_fecha_pub(fp)
        if d and (best is None or d > best):
            best = d
    return best


def sync_incremental(conn, filas):
    """Upsert por nid. Devuelve solo filas nuevas."""
    now = datetime.now(timezone.utc).isoformat()
    nuevas = []
    for f in filas:
        nid = f.get("nid_convocatoria")
        if not nid:
            continue
        exists = conn.execute(
            "SELECT 1 FROM licitaciones WHERE nid_convocatoria=?", (nid,)
        ).fetchone()
        nom_norm = f.get("nomenclatura_norm") or normalizar_nomenclatura(f.get("nomenclatura"))
        f["nomenclatura_norm"] = nom_norm
        conn.execute(
            """
            INSERT INTO licitaciones (
                nid_convocatoria, nomenclatura, entidad, fecha_publicacion,
                objeto, descripcion, vr_ve_cuantia, moneda, version_seace,
                nid_proceso, nid_sistema, nomenclatura_norm, fuente,
                first_seen, last_seen
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(nid_convocatoria) DO UPDATE SET
                nomenclatura=excluded.nomenclatura,
                entidad=excluded.entidad,
                fecha_publicacion=excluded.fecha_publicacion,
                objeto=excluded.objeto,
                descripcion=excluded.descripcion,
                vr_ve_cuantia=excluded.vr_ve_cuantia,
                moneda=excluded.moneda,
                version_seace=excluded.version_seace,
                nid_proceso=excluded.nid_proceso,
                nid_sistema=excluded.nid_sistema,
                nomenclatura_norm=excluded.nomenclatura_norm,
                last_seen=excluded.last_seen
            """,
            (
                nid, f["nomenclatura"], f["entidad"], f["fecha_publicacion"],
                f["objeto"], f["descripcion"], f["vr_ve_cuantia"], f["moneda"],
                f["version_seace"],
                f.get("nid_proceso"), f.get("nid_sistema"),
                nom_norm, f.get("fuente") or "seace",
                now, now,
            ),
        )
        if not exists:
            nuevas.append(f)
    conn.commit()
    return nuevas


# ---------------------------------------------------------------------------
# Ventana de fechas según modo
# ---------------------------------------------------------------------------
def resolver_ventana(conn):
    hoy = date.today()
    if MODO == "backfill":
        print(f"[*] MODO=backfill ventana fija {FECHA_INICIO}..{FECHA_FIN}")
        return FECHA_INICIO, FECHA_FIN

    # sync: ventana corta del radar (48–72h). No se usa la fecha_max histórica de OECE.
    horas = HORAS_RADAR if HORAS_RADAR >= 24 else 72
    ini = (datetime.now() - timedelta(hours=horas)).date()
    print(f"[*] MODO=sync radar {horas}h → {ini}..{hoy}")
    return ini.strftime("%d/%m/%Y"), hoy.strftime("%d/%m/%Y")


def _buscar_pagina(seace, token_holder, fecha_ini, fecha_fin, page_idx, anio, version, objeto):
    """GET fresco + buscar + saltar a page_idx. Renueva captcha si el listado viene vacío."""
    try:
        return _buscar_pagina_inner(
            seace, token_holder, fecha_ini, fecha_fin, page_idx, anio, version, objeto
        )
    except (Timeout, RequestsConnectionError) as e:
        print(
            f"[!] Timeout en SEACE, reintentando o saltando... "
            f"(listado pág.{page_idx + 1}: {e})"
        )
        raise


def _buscar_pagina_inner(seace, token_holder, fecha_ini, fecha_fin, page_idx, anio, version, objeto):
    xml = seace.buscar(token_holder[0], anio, fecha_ini, fecha_fin, version, objeto)
    filas, total = parse_resultados(xml)
    if page_idx == 0:
        time.sleep(SLEEP_SEC)
        xml0 = seace.paginar(0, ROWS)
        filas0, total0 = parse_resultados(xml0)
        if filas0:
            filas, total = filas0, (total0 or total)
    elif page_idx > 0:
        time.sleep(SLEEP_SEC)
        xml = seace.paginar(page_idx * ROWS, ROWS)
        filas_p, total_p = parse_resultados(xml)
        if filas_p:
            filas, total = filas_p, (total_p or total)
    if not filas and page_idx == 0:
        print("[!] Listado vacío — renovando reCAPTCHA...")
        seace.refresh()
        token_holder[0] = obtener_token_recaptcha(seace)
        xml = seace.buscar(token_holder[0], anio, fecha_ini, fecha_fin, version, objeto)
        filas, total = parse_resultados(xml)
        time.sleep(SLEEP_SEC)
        xml0 = seace.paginar(0, ROWS)
        filas0, total0 = parse_resultados(xml0)
        if filas0:
            filas, total = filas0, (total0 or total)
    return filas, total


def barrer_listado(seace, conn, token, fecha_ini, fecha_fin, known_oece=None,
                   anio=None, version=None, objeto=None, max_fichas=None):
    """
    Busca el delta y, en CADA página, abre fichas nuevas ANTES de paginar.
    Así el ViewState del listado sigue vivo para el POST de Ficha (302).
    Tras cada ficha (sale del buscador) se restaura la misma página.
    """
    known_oece = known_oece or set()
    anio = anio or ANIO
    version = version or VERSION_SEACE
    token_holder = [token]
    fichas_ok = 0
    fichas_hechas = set()

    try:
        filas, total = _buscar_pagina(
            seace, token_holder, fecha_ini, fecha_fin, 0, anio, version, objeto
        )
    except (Timeout, RequestsConnectionError, HTTP_EXCEPCIONES) as e:
        print(f"[!] Timeout en SEACE, reintentando o saltando... (página 1: {e})")
        return [], [], 0
    Path("respuesta_test.xml").write_text("", encoding="utf-8")
    print(f"[*] Página 1: {len(filas)} filas | total servidor={total}")
    if not filas and total == 0:
        return [], [], 0

    pages = math.ceil(total / ROWS) if total else 1
    if MAX_PAGES is not None:
        pages = min(pages, MAX_PAGES)
    print(f"[*] Barrido 1..{pages} con ficha-antes-de-paginar (rows={ROWS})")

    todas = []
    nuevas_all = []
    page_idx = 0

    while page_idx < pages:
        if page_idx > 0 and not filas:
            try:
                filas, total = _buscar_pagina(
                    seace, token_holder, fecha_ini, fecha_fin, page_idx,
                    anio, version, objeto,
                )
            except (Timeout, RequestsConnectionError, HTTP_EXCEPCIONES) as e:
                print(
                    f"[!] Timeout en SEACE, reintentando o saltando... "
                    f"(pág.{page_idx + 1}: {e})"
                )
                page_idx += 1
                continue
        if not filas:
            print(f"[-] Página {page_idx + 1} vacía — stop")
            break

        nuevas = sync_incremental(conn, filas)
        todas.extend(filas)
        nuevas_all.extend(nuevas)
        nuevas_nids = {f.get("nid_convocatoria") for f in nuevas if f.get("nid_convocatoria")}
        print(f"[+] pág.{page_idx + 1} +{len(nuevas)} nuevas en DB")

        if PROCESAR_FICHAS_NUEVAS:
            for fila in list(filas):
                nid = fila.get("nid_convocatoria")
                nom = fila.get("nomenclatura_norm") or normalizar_nomenclatura(fila.get("nomenclatura"))
                if nom and nom in known_oece:
                    fila["fuente"] = "ambos"
                    if nid:
                        conn.execute(
                            "UPDATE licitaciones SET fuente=?, bloqueada=1 WHERE nid_convocatoria=?",
                            ("ambos", nid),
                        )
                    print(f"  [=] Duplicado/bloqueada (omitir ficha): {fila.get('nomenclatura')}")
                    continue
                if nid not in nuevas_nids:
                    continue
                fila["fuente"] = "seace"
                try:
                    from supabase_sync import upsert_seace_fila
                    upsert_seace_fila(fila)
                except Exception as e:
                    print(f"  [!] Supabase listado: {e}")
                if not nid or nid in fichas_hechas:
                    continue
                if max_fichas is not None and fichas_ok >= max_fichas:
                    break
                try:
                    print(f"[*] Ficha inmediata {fila.get('nomenclatura')}...")
                    procesar_ficha_y_docs(seace, fila, conn)
                    fichas_hechas.add(nid)
                    fichas_ok += 1
                except HTTP_EXCEPCIONES as e:
                    print(
                        f"  [!] Ficha omitida (timeout/red, se continúa): "
                        f"{fila.get('nomenclatura')} — {e}"
                    )
                    fichas_hechas.add(nid)
                except Exception as e:
                    print(f"  [-] Error ficha {fila.get('nomenclatura')}: {e}")
                    fichas_hechas.add(nid)
                # Volver al listado (el POST de ficha invalidó el ViewState)
                try:
                    seace.refresh()
                    filas, total = _buscar_pagina(
                        seace, token_holder, fecha_ini, fecha_fin, page_idx, anio, version, objeto
                    )
                    pages = math.ceil(total / ROWS) if total else pages
                    if MAX_PAGES is not None:
                        pages = min(pages, MAX_PAGES)
                except (Timeout, RequestsConnectionError, HTTP_EXCEPCIONES) as e:
                    print(
                        f"[!] Timeout en SEACE, reintentando o saltando... "
                        f"(restaurar listado: {e})"
                    )
                    break

        if max_fichas is not None and fichas_ok >= max_fichas:
            print(f"[*] Tope de fichas alcanzado ({max_fichas})")
            break

        page_idx += 1
        if page_idx >= pages:
            break
        time.sleep(SLEEP_SEC)
        print(f"[*] paginar first={page_idx * ROWS} ({page_idx + 1}/{pages})...")
        try:
            xml_p = seace.paginar(page_idx * ROWS, ROWS)
        except (Timeout, RequestsConnectionError, HTTP_EXCEPCIONES) as e:
            print(
                f"[!] Timeout en SEACE, reintentando o saltando... "
                f"(pág.{page_idx + 1} first={page_idx * ROWS}: {e})"
            )
            try:
                seace.refresh()
                filas, total = _buscar_pagina(
                    seace, token_holder, fecha_ini, fecha_fin, page_idx,
                    anio, version, objeto,
                )
            except (Timeout, RequestsConnectionError, HTTP_EXCEPCIONES) as e2:
                print(f"[!] Página {page_idx + 1} omitida; se avanza. ({e2})")
                filas = []
                continue
        else:
            if GUARDAR_XML_PAGINAS:
                Path(f"respuesta_pagina{page_idx + 1}.xml").write_text(
                    xml_p, encoding="utf-8"
                )
            filas, _ = parse_resultados(xml_p)
        if not filas:
            try:
                seace.refresh()
                filas, total = _buscar_pagina(
                    seace, token_holder, fecha_ini, fecha_fin, page_idx,
                    anio, version, objeto,
                )
            except (Timeout, RequestsConnectionError, HTTP_EXCEPCIONES) as e:
                print(
                    f"[!] Timeout en SEACE, reintentando o saltando... "
                    f"(restaurar pág.{page_idx + 1}: {e})"
                )
                filas = []
                continue

    conn.commit()
    return todas, nuevas_all, total


def buscar_fila_en_listado(seace, token, nid_convocatoria, fecha_ini, fecha_fin):
    """Re-busca y pagina hasta encontrar la fila con ese nid (para abrir ficha)."""
    xml = seace.buscar(token, ANIO, fecha_ini, fecha_fin, VERSION_SEACE, OBJETO)
    filas, total = parse_resultados(xml)
    time.sleep(SLEEP_SEC)
    xml0 = seace.paginar(0, ROWS)
    filas0, total0 = parse_resultados(xml0)
    if filas0:
        filas, total = filas0, (total0 or total)

    pages = math.ceil(total / ROWS) if total else 1
    if MAX_PAGES is not None:
        pages = min(pages, MAX_PAGES)

    for page_idx in range(pages):
        if page_idx > 0:
            time.sleep(SLEEP_SEC)
            filas = parse_resultados(seace.paginar(page_idx * ROWS, ROWS))[0]
        for f in filas:
            if f.get("nid_convocatoria") == nid_convocatoria and f.get("ficha_source"):
                return f
    return None


def procesar_ficha_y_docs(seace, fila, conn=None):
    """
    Abre la ficha y registra los documentos disponibles SIN descargarlos.
    - De cada doc se guarda file_code + URL de SeaceWeb-PRO (descarga directa).
    - Si aún no hay ZIP de propuestas (proceso abierto), marca pendiente y sigue.
    """
    nom = fila.get("nomenclatura") or fila.get("nid_proceso") or "sin_nombre"
    nid = fila.get("nid_convocatoria")
    now = datetime.now(timezone.utc).isoformat()

    print(f"[*] Ficha {nom}...")
    time.sleep(SLEEP_SEC)
    url, html = seace.abrir_ficha(fila)
    if GUARDAR_FICHA_HTML:
        FICHA_DIR.mkdir(parents=True, exist_ok=True)
        (FICHA_DIR / f"{re.sub(r'[<>:\"/\\\\|?*]', '_', nom)[:100]}.html").write_text(
            html, encoding="utf-8"
        )

    docs = parse_documentos(html)
    crono_full = extraer_cronograma_prod2(html)
    crono = parse_cronograma_ficha(html)
    estado_ficha = parse_estado_ficha(html) or crono.get("etapa_actual")
    print(
        f"  cronograma={len(crono_full.get('etapas') or [])} etapas | "
        f"actual={crono.get('etapa_actual')}"
    )
    for d in docs:
        d["categoria"] = d.get("categoria") or clasificar_documento(
            d.get("etapa"), d.get("documento"), d.get("nombre_archivo")
        )
        d["file_code"] = d.get("file_code") or d.get("file_id") or ""
        href = (d.get("url_descarga") or "").strip()
        if href and "downloadDoc" not in href:
            d["url_descarga"] = href
        else:
            d["url_descarga"] = construir_url_descarga_prod2(d["file_code"]) or href
    elegido = elegir_documento_prioridad(docs)
    cats = {d["categoria"] for d in docs}
    tiene_bases = bool(elegido)
    tiene_prop = "presentacion_propuestas" in cats
    prop_pendiente = 0 if tiene_prop else 1

    print(
        f"  docs={len(docs)} "
        f"(prioridad={elegido['categoria'] if elegido else 'ninguna'}, "
        f"propuestas={'sí' if tiene_prop else 'PENDIENTE'}) | {url}"
    )

    url_bases = ""
    file_code = ""
    if elegido:
        etiqueta = elegido.get("documento") or elegido.get("nombre_archivo")
        file_code = elegido.get("file_code") or ""
        url_bases = elegido.get("url_descarga") or ""
        print(f"    [{elegido['categoria']}] {etiqueta} fileCode={file_code}")
        print(f"      URL (on-demand): {url_bases}")

    if conn and nid:
        for d in docs:
            if not d.get("file_id"):
                continue
            conn.execute(
                """
                INSERT INTO documentos (
                    file_id, nid_convocatoria, categoria, etapa, documento,
                    nombre_archivo, file_code, url_descarga, registrado_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(file_id) DO UPDATE SET
                    categoria=excluded.categoria,
                    file_code=excluded.file_code,
                    url_descarga=excluded.url_descarga,
                    registrado_at=excluded.registrado_at
                """,
                (
                    d["file_id"], nid, d["categoria"], d.get("etapa"),
                    d.get("documento"), d.get("nombre_archivo"),
                    d.get("file_code"), d.get("url_descarga"), now,
                ),
            )

    if conn and nid:
        prox = None
        if prop_pendiente:
            prox = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        conn.execute(
            """
            UPDATE licitaciones SET
                ficha_url=?,
                docs_bases_ok=?,
                docs_propuestas_ok=?,
                docs_propuestas_pendiente=?,
                proxima_revision=?,
                url_bases=?,
                file_code=?
            WHERE nid_convocatoria=?
            """,
            (
                url,
                1 if tiene_bases else 0,
                1 if tiene_prop else 0,
                prop_pendiente,
                prox,
                url_bases,
                file_code,
                nid,
            ),
        )
        conn.commit()

    try:
        from supabase_sync import upsert_documentos, upsert_seace_fila
        nom_norm = upsert_seace_fila(fila, extra={
            "ficha_url": url,
            "url_bases": url_bases,
            "file_code": file_code,
            "estado": estado_ficha or fila.get("estado"),
            **crono,
        })
        if nom_norm:
            upsert_documentos(nom_norm, docs)
    except Exception as e:
        print(f"  [!] Supabase ficha: {e}")

    return url, docs


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description="Delta SEACE PROD2: radar 48–72h + ficha solo de nomenclaturas nuevas no bloqueadas"
    )
    p.add_argument("--desde", help="Fecha inicio DD/MM/YYYY o YYYY-MM-DD (default: radar 72h)")
    p.add_argument("--hasta", help="Fecha fin (default: hoy)")
    p.add_argument("--year", default=ANIO)
    p.add_argument("--objeto", default=OBJETO, help="Bien|Obra|Servicio|Consultoría de Obra. Vacío=todos")
    p.add_argument("--nomenclaturas-file", default=HANDOFF_DEFAULT or None, help="JSON/TXT de nomenclaturas OECE para deduplicar")
    p.add_argument("--max-fichas", type=int, default=MAX_FICHAS_POR_CORRIDA)
    p.add_argument("--modo", default=MODO, choices=("sync", "backfill"))
    p.add_argument(
        "--horas-radar",
        type=int,
        default=None,
        help="Ventana de publicación en horas (default EMBUDO_HORAS_RADAR o 72)",
    )
    p.add_argument(
        "--dry-run-cronograma",
        action="store_true",
        help="Solo GET de una fichaSeleccion y parsea las 8 etapas (no toca BD)",
    )
    p.add_argument(
        "--ficha-url",
        default=FICHA_DRY_RUN_URL,
        help="URL de fichaSeleccion.xhtml para --dry-run-cronograma",
    )
    return p.parse_args()


def dry_run_cronograma(url=None):
    """
    No usa requests.get suelto. Calienta Seace (buscador + JSESSIONID + ViewState)
    y recién entonces pide fichaSeleccion.xhtml con esa misma Session.
    """
    url = url or FICHA_DRY_RUN_URL
    print(f"[*] Dry-run cronograma PROD2 → {url}")
    print("[*] 1/2 Calentando sesión JSF: GET buscadorPublico.xhtml")
    seace = Seace(calentar=False)
    seace.refresh()
    if not seace.sesion_caliente():
        raise SystemExit(
            "[-] Sesión no inicializada: falta JSESSIONID o ViewState."
        )
    print("[*] 2/2 GET fichaSeleccion.xhtml con la sesión viva")
    final_url, html = seace.get_ficha_url(url)
    data = extraer_cronograma_prod2(html)
    payload = {
        "url": final_url,
        "html_ok": "tbFicha:dtCronograma_data" in (html or ""),
        "html_bytes": len(html or ""),
        "etapas": data.get("etapas") or [],
        "mapeo_convocatorias": data.get("mapeo_convocatorias") or {},
        "etapa_actual": data.get("etapa_actual"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    args = parse_args()
    if args.dry_run_cronograma:
        dry_run_cronograma(args.ficha_url)
        sys.exit(0)
    detectar_ip_publica()
    known_oece = cargar_nomenclaturas_oece(args.nomenclaturas_file)
    objeto = args.objeto
    if objeto is not None and objeto.strip() == "":
        objeto = None

    if args.horas_radar:
        globals()["HORAS_RADAR"] = max(24, int(args.horas_radar))

    print(
        f"=== SEACE PROD2 | MODO={args.modo} | ROWS={ROWS} | "
        f"radar={HORAS_RADAR}h | max_fichas={args.max_fichas or 'ilimitado'} ==="
    )
    conn = db_connect()
    print(f"[*] DB {DB_FILE.name}: {db_count(conn)} registros")

    if args.desde:
        fecha_ini = fecha_iso_a_seace(args.desde)
        fecha_fin = fecha_iso_a_seace(args.hasta) if args.hasta else date.today().strftime("%d/%m/%Y")
        print(f"[*] Ventana explícita: {fecha_ini} .. {fecha_fin}")
    else:
        fecha_ini, fecha_fin = resolver_ventana(conn)

    seace = Seace()
    token = obtener_token_recaptcha(seace)

    todas, nuevas, total = barrer_listado(
        seace, conn, token, fecha_ini, fecha_fin,
        known_oece=known_oece,
        anio=str(args.year),
        objeto=objeto,
        max_fichas=args.max_fichas,
    )

    NUEVAS_FILE.write_text(
        json.dumps(nuevas, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    Path("resultados_barrido.json").write_text(
        json.dumps(todas, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    Path("seace_delta_resumen.json").write_text(
        json.dumps({
            "fuente": "PROD2",
            "fecha_ini": fecha_ini,
            "fecha_fin": fecha_fin,
            "vistas": len(todas),
            "nuevas": len(nuevas),
            "duplicados_oece": sum(1 for f in todas if f.get("fuente") == "ambos"),
            "solo_seace": sum(1 for f in todas if f.get("fuente") != "ambos"),
            "db": db_count(conn),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("---")
    print(f"[+] Vistas: {len(todas)} | servidor≈{total} | nuevas={len(nuevas)} | DB={db_count(conn)}")
    print(f"[+] Duplicados OECE (nomenclatura): {sum(1 for f in todas if f.get('fuente') == 'ambos')}")
    print(f"[+] Solo SEACE (delta): {sum(1 for f in todas if f.get('fuente') != 'ambos')}")
    print(f"[+] PROD2 | procesos nuevos inyectados: {len(nuevas)}")
    print("[+] Documentos: solo URL Alfresco (sin descarga de binarios)")
