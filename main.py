"""
SEACE PROD2 — listado HTTP (PrimeFaces AJAX) + SQLite incremental.

Modos:
  backfill → ventana fija (FECHA_INICIO/FIN), barra todas las páginas.
  sync     → ventana corta desde la DB (rápido, solo detecta nuevas).

tokenBusProSel: pegar fresco en token.txt antes de cada corrida.
"""
from __future__ import annotations

from pathlib import Path
import json
import math
import re
import sqlite3
import time
import warnings
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

FORM = "tbBuscador:idFormBuscarProceso"
DT = f"{FORM}:dtProcesos"
HOST = "https://prod2.seace.gob.pe"
ALFRESCO = "https://alfprod.seace.gob.pe/alfresco"
IP_CLIENTE = "179.43.89.146"
TOKEN_FILE = Path(__file__).with_name("token.txt")
DB_FILE = Path(__file__).with_name("seace.db")
NUEVAS_FILE = Path(__file__).with_name("nuevas.json")

# --- Modo ---
# "backfill" = carga histórica de la ventana fija (una vez).
# "sync"     = incremental diario/horario (minutos).
MODO = "sync"

# --- Filtros ---
ANIO = "2026"
FECHA_INICIO = "01/08/2026"   # solo backfill (o fallback si DB vacía)
FECHA_FIN = "31/08/2026"
VERSION_SEACE = "Seace 3"
# "Bien" | "Obra" | "Consultoría de Obra" | "Servicio" | None
OBJETO = "Servicio"

# --- Sync: ventana hacia atrás desde hoy / desde max(fecha) en DB ---
DIAS_LOOKBACK = 3
DIAS_SOLAPE = 1

# --- Paginación / ritmo ---
ROWS = 20
SLEEP_SEC = 1.2
MAX_PAGES = None
GUARDAR_XML_PAGINAS = False

# --- Ficha / descarga ---
PROBAR_FICHA = False
PROBAR_DESCARGA = False
# Tras sync/backfill: abrir ficha y bajar docs solo de nids nuevos
PROCESAR_FICHAS_NUEVAS = True
MAX_FICHAS_POR_CORRIDA = 5   # tope por corrida (respeta servidor); None = todas las nuevas
FICHA_DIR = Path(__file__).with_name("fichas")
ARCHIVOS_DIR = FICHA_DIR / "archivos"


# ---------------------------------------------------------------------------
# Cliente SEACE
# ---------------------------------------------------------------------------
class Seace:
    def __init__(self, host=HOST):
        self.host = host
        self.page = f"{host}/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml"
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9",
        })
        self._vs = None
        self.filtros = {}
        self.refresh()

    def refresh(self):
        r = self.s.get(self.page, timeout=60)
        r.raise_for_status()
        self.soup = BeautifulSoup(r.text, "lxml")
        vs = self.soup.find("input", {"name": "javax.faces.ViewState"})
        if not vs:
            Path("error_get.html").write_text(r.text, encoding="utf-8")
            raise ValueError("No se pudo obtener el ViewState inicial.")
        self._vs = vs["value"]
        print(f"[+] ViewState: {self._vs[:20]}...")

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
        r = self.s.post(self.page, data=data, headers=self._headers_ajax(), timeout=120)
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

        r = self.s.post(self.page, data=data, headers=self._headers_ajax(), timeout=120)
        r.raise_for_status()
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
        r = self.s.post(
            self.page, data=data, headers=headers,
            allow_redirects=False, timeout=120,
        )
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
            raise RuntimeError(f"Redirect inesperado: {loc}")

        rf = self.s.get(loc, headers={
            "User-Agent": self.s.headers.get("User-Agent"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": self.page,
        }, timeout=120)
        rf.raise_for_status()
        return loc, rf.text

    def descargar_documento(self, file_id, nombre_sugerido=None, dest_dir=None):
        """
        Flujo cmsDescarga.js / descargaPriv (captura DevTools):
          1) GET alfresco/.../downloadDoc?id={uuid}&callback=...  (JSONP)
          2) GET alfresco + downloadUrl (+ alf_ticket)
        El POST AJAX a fichaSeleccion NO trae el archivo (solo ViewState).
        """
        dest_dir = Path(dest_dir or ARCHIVOS_DIR)
        dest_dir.mkdir(parents=True, exist_ok=True)

        cb = f"c{int(time.time() * 1000) % 10_000_000_000}"
        meta_url = (
            f"{ALFRESCO}/service/osce/downloadDoc"
            f"?id={file_id}&doc={cb}&guest=false&callback={cb}&_={int(time.time() * 1000)}"
        )
        headers = {
            "User-Agent": self.s.headers.get("User-Agent"),
            "Accept": "*/*",
            "Referer": f"{self.host}/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml",
        }
        r = self.s.get(meta_url, headers=headers, timeout=60)
        r.raise_for_status()

        m = re.search(r"^[^(]+\((.*)\)\s*;?\s*$", r.text.strip(), re.S)
        if not m:
            raise RuntimeError(f"JSONP inesperado de downloadDoc: {r.text[:200]}")
        payload = json.loads(m.group(1))
        if str(payload.get("result")) != "200" or not payload.get("downloadUrl"):
            raise RuntimeError(f"downloadDoc falló: {payload}")

        rel = payload["downloadUrl"]
        file_url = rel if rel.startswith("http") else (ALFRESCO.rstrip("/") + rel)

        # Nombre desde URL (.../SpacesStore/{uuid}/NOMBRE.zip?a=true&alf_ticket=...)
        url_name = rel.split("?")[0].rstrip("/").split("/")[-1]
        raw_name = nombre_sugerido or url_name or f"{file_id}.bin"
        safe = re.sub(r'[<>:"/\\|?*]', "_", raw_name).strip() or f"{file_id}.bin"
        out = dest_dir / safe

        rf = self.s.get(file_url, headers={
            "User-Agent": self.s.headers.get("User-Agent"),
            "Accept": "*/*",
            "Referer": f"{self.host}/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml",
        }, timeout=180)
        rf.raise_for_status()
        out.write_bytes(rf.content)
        return out, len(rf.content), file_url


# ---------------------------------------------------------------------------
# Parseo
# ---------------------------------------------------------------------------
def load_token():
    if not TOKEN_FILE.exists():
        raise SystemExit(f"[-] Falta {TOKEN_FILE.name} — pega tokenBusProSel de DevTools")
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if len(token) < 100:
        raise SystemExit("[-] token.txt vacío/incompleto")
    print(f"[+] Token len={len(token)}")
    return token


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


def parse_documentos(ficha_html):
    """
    Extrae docs de dtDocumentos.
    Preferimos el link con descargaDocGeneral(uuid, tipo, nombre).
    Misma vía Alfresco para Bases y para Documentos de Presentación de Propuestas.
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
                "categoria": clasificar_documento(None, None, nombre),
            })
        return docs

    for tr in tbody.find_all("tr", recursive=False):
        if "ui-datatable-empty-message" in (tr.get("class") or []):
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue
        file_id = tipo = nombre = source = None
        for a in tr.find_all("a"):
            oc = a.get("onclick") or ""
            m = re.search(
                r"descargaDocGeneral\('([^']*)','([^']*)','([^']*)'\)", oc
            )
            if m:
                file_id, tipo, nombre = m.group(1), m.group(2), m.group(3)
                source = a.get("id")
                break
        if not file_id:
            continue
        etapa = tds[1].get_text(" ", strip=True)
        documento = tds[2].get_text(" ", strip=True)
        docs.append({
            "n": tds[0].get_text(" ", strip=True),
            "etapa": etapa,
            "documento": documento,
            "file_id": file_id,
            "tipo": tipo,
            "nombre_archivo": nombre,
            "fuente": source,
            "fecha": tds[4].get_text(" ", strip=True) if len(tds) > 4 else None,
            "categoria": clasificar_documento(etapa, documento, nombre),
        })
    return docs


def clasificar_documento(etapa, documento, nombre_archivo):
    """Clasifica doc publicado en ficha (puede no existir aún si el proceso sigue abierto)."""
    blob = " ".join(
        x.lower() for x in (etapa or "", documento or "", nombre_archivo or "")
    )
    if any(k in blob for k in (
        "presentacion de propuesta", "presentación de propuesta",
        "documentos de presentacion", "documentos de presentación",
        "presentacion_de_propuestas", "presentación de ofertas",
        "presentacion de ofertas",
    )):
        return "presentacion_propuestas"
    if "base" in blob and ("administrativ" in blob or "estandar" in blob or "estándar" in blob):
        return "bases"
    if blob.strip().startswith("convocatoria") and "base" in blob:
        return "bases"
    if "base" in blob:
        return "bases"
    return "otro"


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
            ruta_local TEXT,
            bytes INTEGER,
            descargado_at TEXT,
            FOREIGN KEY(nid_convocatoria) REFERENCES licitaciones(nid_convocatoria)
        )
        """
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
        conn.execute(
            """
            INSERT INTO licitaciones (
                nid_convocatoria, nomenclatura, entidad, fecha_publicacion,
                objeto, descripcion, vr_ve_cuantia, moneda, version_seace,
                nid_proceso, nid_sistema,
                first_seen, last_seen
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                last_seen=excluded.last_seen
            """,
            (
                nid, f["nomenclatura"], f["entidad"], f["fecha_publicacion"],
                f["objeto"], f["descripcion"], f["vr_ve_cuantia"], f["moneda"],
                f["version_seace"],
                f.get("nid_proceso"), f.get("nid_sistema"),
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

    # sync
    mx = db_max_fecha(conn)
    if mx is None:
        ini = hoy - timedelta(days=DIAS_LOOKBACK)
        print(f"[*] MODO=sync DB vacía → lookback {DIAS_LOOKBACK}d: {ini}..{hoy}")
    else:
        ini = mx - timedelta(days=DIAS_SOLAPE)
        print(f"[*] MODO=sync desde DB max={mx} −{DIAS_SOLAPE}d → {ini}..{hoy}")
    return ini.strftime("%d/%m/%Y"), hoy.strftime("%d/%m/%Y")


def barrer_listado(seace, conn, token, fecha_ini, fecha_fin):
    """Buscar + paginar toda la ventana. Devuelve (todas_filas, nuevas, total_servidor)."""
    xml = seace.buscar(token, ANIO, fecha_ini, fecha_fin, VERSION_SEACE, OBJETO)
    Path("respuesta_test.xml").write_text(xml, encoding="utf-8")

    filas, total = parse_resultados(xml)
    print(f"[*] Página búsqueda: {len(filas)} filas | total servidor={total}")
    if not filas and total == 0:
        return [], [], 0

    # Forzar ROWS en el paginador (el buscar suele dejar 15)
    time.sleep(SLEEP_SEC)
    xml0 = seace.paginar(0, ROWS)
    filas0, total0 = parse_resultados(xml0)
    if filas0:
        filas, total = filas0, (total0 or total)
        print(f"[*] Re-página 1 con rows={ROWS}: {len(filas)} filas | total={total}")
    elif not filas:
        return [], [], 0

    todas = []
    nuevas_all = []
    nuevas = sync_incremental(conn, filas)
    todas.extend(filas)
    nuevas_all.extend(nuevas)
    print(f"[+] pág.1 +{len(nuevas)} nuevas")

    pages = math.ceil(total / ROWS) if total else 1
    if MAX_PAGES is not None:
        pages = min(pages, MAX_PAGES)
    print(f"[*] Barrido 2..{pages} (rows={ROWS}, sleep={SLEEP_SEC}s)")

    # Early-stop en sync: N páginas seguidas sin nuevas
    racha_sin_nuevas = 0
    EARLY_STOP = 2 if MODO == "sync" else None

    for page_idx in range(1, pages):
        first = page_idx * ROWS
        time.sleep(SLEEP_SEC)
        print(f"[*] paginar first={first} ({page_idx + 1}/{pages})...")
        xml_p = seace.paginar(first, ROWS)
        if GUARDAR_XML_PAGINAS:
            Path(f"respuesta_pagina{page_idx + 1}.xml").write_text(xml_p, encoding="utf-8")
        filas_p, _ = parse_resultados(xml_p)
        if not filas_p:
            print(f"[-] Página {page_idx + 1} vacía — stop")
            break
        nuevas_p = sync_incremental(conn, filas_p)
        todas.extend(filas_p)
        nuevas_all.extend(nuevas_p)
        print(
            f"  +{len(filas_p)} filas | +{len(nuevas_p)} nuevas | "
            f"N° {filas_p[0]['n']}..{filas_p[-1]['n']}"
        )
        if EARLY_STOP is not None:
            if nuevas_p:
                racha_sin_nuevas = 0
            else:
                racha_sin_nuevas += 1
                if racha_sin_nuevas >= EARLY_STOP:
                    print(f"[*] Early-stop sync: {EARLY_STOP} páginas sin novedades")
                    break

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
    Abre ficha, guarda HTML, descarga todos los docs Alfresco disponibles.
    - Bases y 'Documentos de Presentación de Propuestas' usan el MISMO flujo.
    - Si aún no hay ZIP de propuestas (proceso abierto), marca pendiente y sigue.
    """
    nom = fila.get("nomenclatura") or fila.get("nid_proceso") or "sin_nombre"
    nid = fila.get("nid_convocatoria")
    sub = ARCHIVOS_DIR / re.sub(r'[<>:"/\\|?*]', "_", nom)[:100]
    sub.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()

    print(f"[*] Ficha {nom}...")
    time.sleep(SLEEP_SEC)
    url, html = seace.abrir_ficha(fila)
    (FICHA_DIR / f"{re.sub(r'[<>:\"/\\\\|?*]', '_', nom)[:100]}.html").write_text(
        html, encoding="utf-8"
    )

    docs = parse_documentos(html)
    cats = {d["categoria"] for d in docs}
    tiene_bases = "bases" in cats
    tiene_prop = "presentacion_propuestas" in cats
    # Si el proceso sigue abierto, ese doc simplemente no aparece en dtDocumentos
    prop_pendiente = 0 if tiene_prop else 1

    print(
        f"  docs={len(docs)} "
        f"(bases={'sí' if tiene_bases else 'no'}, "
        f"propuestas={'sí' if tiene_prop else 'PENDIENTE'}) | {url}"
    )

    guardados = []
    for d in docs:
        etiqueta = d.get("documento") or d.get("nombre_archivo")
        print(f"    [{d['categoria']}] {etiqueta}")
        time.sleep(SLEEP_SEC)
        path, nbytes, _ = seace.descargar_documento(
            d["file_id"], d.get("nombre_archivo"), sub
        )
        print(f"      → {path.name} ({nbytes} bytes)")
        guardados.append(str(path))
        if conn and nid:
            conn.execute(
                """
                INSERT INTO documentos (
                    file_id, nid_convocatoria, categoria, etapa, documento,
                    nombre_archivo, ruta_local, bytes, descargado_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(file_id) DO UPDATE SET
                    ruta_local=excluded.ruta_local,
                    bytes=excluded.bytes,
                    descargado_at=excluded.descargado_at,
                    categoria=excluded.categoria
                """,
                (
                    d["file_id"], nid, d["categoria"], d.get("etapa"),
                    d.get("documento"), d.get("nombre_archivo"),
                    str(path), nbytes, now,
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
                proxima_revision=?
            WHERE nid_convocatoria=?
            """,
            (
                url,
                1 if tiene_bases else 0,
                1 if tiene_prop else 0,
                prop_pendiente,
                prox,
                nid,
            ),
        )
        conn.commit()

    return url, guardados


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(
        f"=== SEACE | MODO={MODO} | ROWS={ROWS} | "
        f"FICHAS_NUEVAS={PROCESAR_FICHAS_NUEVAS} | max={MAX_FICHAS_POR_CORRIDA} ==="
    )
    token = load_token()
    conn = db_connect()
    print(f"[*] DB {DB_FILE.name}: {db_count(conn)} registros")

    fecha_ini, fecha_fin = resolver_ventana(conn)
    seace = Seace()

    if PROBAR_DESCARGA:
        ficha_path = FICHA_DIR / "ficha_test.html"
        if ficha_path.exists():
            html = ficha_path.read_text(encoding="utf-8", errors="replace")
            print(f"[*] Usando ficha local {ficha_path}")
        else:
            xml = seace.buscar(token, ANIO, fecha_ini, fecha_fin, VERSION_SEACE, OBJETO)
            filas, _ = parse_resultados(xml)
            if not filas:
                filas, _ = parse_resultados(seace.paginar(0, ROWS))
            fila = next((f for f in filas if f.get("ficha_source")), None)
            if not fila:
                raise SystemExit("[-] Sin fila/ficha para descarga")
            time.sleep(SLEEP_SEC)
            _url, html = seace.abrir_ficha(fila)
            FICHA_DIR.mkdir(exist_ok=True)
            ficha_path.write_text(html, encoding="utf-8")

        docs = parse_documentos(html)
        print(f"[*] Documentos en ficha: {len(docs)}")
        if not docs:
            raise SystemExit("[-] No hay descargaDocGeneral en la ficha")
        for d in docs:
            print(f"  - {d.get('documento') or d['nombre_archivo']} | id={d['file_id'][:8]}...")
            time.sleep(SLEEP_SEC)
            path, nbytes, url = seace.descargar_documento(
                d["file_id"], d.get("nombre_archivo"), ARCHIVOS_DIR
            )
            print(f"    → {path.name} ({nbytes} bytes)")
        print(f"[+] Listo. Archivos en {ARCHIVOS_DIR}")
        raise SystemExit(0)

    if PROBAR_FICHA:
        xml = seace.buscar(token, ANIO, fecha_ini, fecha_fin, VERSION_SEACE, OBJETO)
        Path("respuesta_test.xml").write_text(xml, encoding="utf-8")
        filas, total = parse_resultados(xml)
        if not filas:
            time.sleep(SLEEP_SEC)
            filas, total = parse_resultados(seace.paginar(0, ROWS))
        fila = next((f for f in filas if f.get("ficha_source")), None)
        if not fila:
            raise SystemExit("[-] Sin ficha_source")
        url, html = seace.abrir_ficha(fila)
        FICHA_DIR.mkdir(exist_ok=True)
        (FICHA_DIR / "ficha_test.html").write_text(html, encoding="utf-8")
        print(f"[+] Ficha OK → {url}")
        raise SystemExit(0)

    todas, nuevas, total = barrer_listado(seace, conn, token, fecha_ini, fecha_fin)

    NUEVAS_FILE.write_text(
        json.dumps(nuevas, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    Path("resultados_barrido.json").write_text(
        json.dumps(todas, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("---")
    print(f"[+] Vistas: {len(todas)} | servidor≈{total} | nuevas={len(nuevas)} | DB={db_count(conn)}")

    if PROCESAR_FICHAS_NUEVAS and nuevas:
        cola = nuevas
        if MAX_FICHAS_POR_CORRIDA is not None:
            cola = nuevas[:MAX_FICHAS_POR_CORRIDA]
        print(f"[*] Procesando fichas/docs de {len(cola)}/{len(nuevas)} nuevas...")
        for i, meta in enumerate(cola):
            nid = meta.get("nid_convocatoria")
            try:
                if i == 0 and meta.get("ficha_source"):
                    # Tras el listado el ViewState del buscador sigue vivo
                    fila = meta
                else:
                    seace.refresh()
                    fila = buscar_fila_en_listado(
                        seace, token, nid, fecha_ini, fecha_fin
                    )
                if not fila or not fila.get("ficha_source"):
                    print(f"  [-] No se reubicó {meta.get('nomenclatura')}")
                    continue
                procesar_ficha_y_docs(seace, fila, conn)
            except Exception as e:
                print(f"  [-] Error {meta.get('nomenclatura')}: {e}")
        print(f"[+] Archivos en {ARCHIVOS_DIR}")
    elif PROCESAR_FICHAS_NUEVAS:
        print("[*] Sin nuevas → no hay fichas que abrir (sync incremental OK).")
