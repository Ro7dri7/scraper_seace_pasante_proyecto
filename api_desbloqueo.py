"""
LicitApp — microservicio on-demand de desbloqueo ISO.

El frontend / n8n llama POST /api/v1/analizar-iso cuando el usuario gasta un
crédito. Aquí se descarga en memoria el documento prioritario de Alfresco
(Bases Integradas > Bases Administrativas), se recorta el texto a las
secciones de calificación/ISO y se pregunta al LLM si el Estado exige una
certificación. El JSON resultante se persiste en convocatorias.requiere_iso.

Nunca escribe el binario a disco.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from lib_nomenclatura import normalizar_nomenclatura
from supabase_sync import construir_url_descarga_prod2, get_client, load_env

load_env()

log = logging.getLogger("licitapp.desbloqueo")
if not log.handlers:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    return default if val is None else val.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, "")
    return default if raw == "" else int(raw)


ALFRESCO_BASE = (
    _env("SEACE_ALFRESCO") or "https://alfprod.seace.gob.pe/alfresco"
).rstrip("/")
SEACE_HOST = _env("SEACE_HOST") or "https://prod2.seace.gob.pe"
# Cliente OpenAI-compatible: OpenAI oficial, OpenRouter, KeyAI, etc.
LLM_BASE_URL = _env("LLM_BASE_URL")  # vacío = https://api.openai.com/v1
LLM_API_KEY = _env("LLM_API_KEY") or _env("OPENAI_API_KEY")
LLM_MODEL = _env("LLM_MODEL") or "gpt-4o-mini"
LLM_TIMEOUT = _env_int("LLM_TIMEOUT", 60)
LLM_MAX_RETRIES = _env_int("LLM_MAX_RETRIES", 2)
MAX_BYTES = _env_int("DESBLOQUEO_MAX_BYTES", 40 * 1024 * 1024)
MAX_PALABRAS = _env_int("DESBLOQUEO_MAX_PALABRAS", 1000)
HTTP_TIMEOUT = _env_int("DESBLOQUEO_HTTP_TIMEOUT", 90)

PRIORIDAD_CATEGORIA: dict[str, int] = {
    "bases_integradas": 100,
    "bases_administrativas": 50,
}

# Secciones / normas que justifican mandar texto al LLM.
RE_SECCIONES = re.compile(
    r"(requisitos?\s+de\s+calificaci[oó]n|"
    r"factores?\s+(?:de\s+)?(?:evaluaci[oó]n|calificaci[oó]n)|"
    r"certificaci(?:[oó]n|ones)|"
    r"sistemas?\s+de\s+gesti[oó]n|"
    r"norma(?:s)?\s+iso|"
    r"iso\s*9[\s.\-]*001|"
    r"iso\s*14[\s.\-]*001|"
    r"iso\s*27[\s.\-]*001|"
    r"iso\s*37[\s.\-]*001|"
    r"iso\s*45[\s.\-]*001|"
    r"iso\s*\d{4,5}|"
    r"ohsas|"
    r"antisoborno|"
    r"gesti[oó]n\s+de\s+calidad)",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """Eres un analista de contrataciones públicas del Estado peruano (SEACE/OECE).
Analizas únicamente el fragmento de Bases Administrativas o Bases Integradas que se te entrega.
Debes determinar si el Estado exige al postor alguna certificación ISO o un Sistema de Gestión equivalente.

INSTRUCCIONES DE FORMATO (OBLIGATORIAS):
- Responde SOLO un objeto JSON válido parseable por json.loads.
- Prohibido markdown, fences (```), comentarios, preámbulos o texto fuera del JSON.
- Prohibido claves extra. El objeto debe tener exactamente estas claves:
{"requiere_iso": true, "normas_identificadas": ["ISO 9001"], "condicion": "Obligatorio", "resumen_requisito": "..."}

Reglas de negocio:
- requiere_iso es true solo si el texto exige, solicita o puntúa una certificación ISO / sistema de gestión.
- condicion: únicamente "Obligatorio" (requisito de calificación o no admisión), "Puntaje" (solo otorga puntaje) o "No aplica".
- normas_identificadas: códigos ISO mencionados (ej. ISO 9001, ISO 37001). Lista vacía si no hay.
- resumen_requisito: 1 a 3 oraciones citando el requisito, o "No se identificó exigencia ISO."
- No inventes normas que no estén en el texto. Si el fragmento es insuficiente: requiere_iso=false y condicion="No aplica".
"""


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------
class AnalizarIsoRequest(BaseModel):
    nomenclatura_norm: str = Field(..., min_length=4, max_length=120)

    @field_validator("nomenclatura_norm")
    @classmethod
    def _normalizar(cls, v: str) -> str:
        norm = normalizar_nomenclatura(v)
        if len(norm) < 4:
            raise ValueError("nomenclatura_norm inválida")
        return norm


class DocumentoUsado(BaseModel):
    file_id: str | None = None
    file_code: str | None = None
    categoria: str
    documento: str | None = None
    nombre_archivo: str | None = None
    url_descarga: str


class AnalisisIso(BaseModel):
    requiere_iso: bool
    normas_identificadas: list[str] = Field(default_factory=list)
    condicion: Literal["Obligatorio", "Puntaje", "No aplica"]
    resumen_requisito: str


class AnalizarIsoResponse(BaseModel):
    nomenclatura_norm: str
    documento: DocumentoUsado
    analisis: AnalisisIso
    paginas_analizadas: list[int] = Field(default_factory=list)
    palabras_enviadas: int = 0
    llm_invocado: bool = False


class ErrorBody(BaseModel):
    detail: str
    codigo: str
    nomenclatura_norm: str | None = None


# ---------------------------------------------------------------------------
# Excepciones de dominio → HTTP
# ---------------------------------------------------------------------------
class DesbloqueoError(Exception):
    def __init__(self, codigo: str, mensaje: str, status: int = 500):
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje
        self.status = status


# ---------------------------------------------------------------------------
# 1) Documento prioritario en Supabase
# ---------------------------------------------------------------------------
def _score_documento(row: dict[str, Any]) -> int:
    cat = (row.get("categoria") or "").strip().lower()
    if cat in PRIORIDAD_CATEGORIA:
        return PRIORIDAD_CATEGORIA[cat]
    blob = " ".join(
        str(row.get(k) or "") for k in ("documento", "nombre_archivo", "categoria")
    ).lower()
    if "bases integradas" in blob:
        return PRIORIDAD_CATEGORIA["bases_integradas"]
    if "bases administrativas" in blob or (
        "base" in blob and "administrativ" in blob
    ):
        return PRIORIDAD_CATEGORIA["bases_administrativas"]
    return 0


def buscar_documento_prioritario(nom_norm: str) -> DocumentoUsado:
    """
    Bases Integradas > Bases Administrativas.
    Si no hay fila en documentos_proceso, cae a convocatorias.file_code/url_bases.
    """
    client = get_client()
    try:
        res = (
            client.table("documentos_proceso")
            .select(
                "file_id,file_code,categoria,documento,nombre_archivo,url_descarga,nomenclatura_norm"
            )
            .eq("nomenclatura_norm", nom_norm)
            .execute()
        )
    except Exception as e:
        raise DesbloqueoError(
            "supabase_docs", f"Error al consultar documentos_proceso: {e}", 502
        ) from e

    mejor: dict[str, Any] | None = None
    mejor_score = 0
    for row in res.data or []:
        score = _score_documento(row)
        if score > mejor_score:
            mejor, mejor_score = row, score

    if mejor and mejor_score > 0:
        file_code = mejor.get("file_code") or mejor.get("file_id") or ""
        url = (mejor.get("url_descarga") or "").strip()
        if not url or "downloadDoc" in url:
            url = construir_url_descarga_prod2(file_code) or url
        if not url:
            raise DesbloqueoError(
                "sin_url",
                "El documento prioritario no tiene url_descarga ni file_code.",
                404,
            )
        log.info(
            "Documento prioritario %s categoria=%s file_code=%s",
            nom_norm, mejor.get("categoria"), file_code,
        )
        return DocumentoUsado(
            file_id=mejor.get("file_id"),
            file_code=file_code or None,
            categoria=str(mejor.get("categoria") or ""),
            documento=mejor.get("documento"),
            nombre_archivo=mejor.get("nombre_archivo"),
            url_descarga=url,
        )

    try:
        conv = (
            client.table("convocatorias")
            .select("nomenclatura_norm,file_code,url_bases")
            .eq("nomenclatura_norm", nom_norm)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise DesbloqueoError(
            "supabase_conv", f"Error al consultar convocatorias: {e}", 502
        ) from e

    fila = (conv.data or [None])[0]
    if not fila:
        raise DesbloqueoError(
            "convocatoria_ausente",
            f"No existe la convocatoria {nom_norm} en Supabase.",
            404,
        )
    file_code = (fila.get("file_code") or "").strip()
    url = (fila.get("url_bases") or "").strip()
    if not url or "downloadDoc" in url:
        url = construir_url_descarga_prod2(file_code) or url
    if not url:
        raise DesbloqueoError(
            "sin_documento",
            "No hay Bases Integradas ni Bases Administrativas para esta nomenclatura.",
            404,
        )
    log.info("Fallback convocatorias.url_bases/file_code para %s", nom_norm)
    return DocumentoUsado(
        file_code=file_code or None,
        categoria="bases_administrativas",
        documento="Bases (fallback convocatoria)",
        url_descarga=url,
    )


# ---------------------------------------------------------------------------
# 2) Descarga Alfresco en memoria (JSONP → alf_ticket → bytes)
# ---------------------------------------------------------------------------
def _headers_alfresco() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": f"{SEACE_HOST}/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml",
    }


def _con_callback(url: str) -> str:
    """El resolver downloadDoc espera JSONP; inyecta callback si falta."""
    parsed = urlparse(url)
    if "downloadDoc" not in (parsed.path or ""):
        return url
    qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "callback" not in qs:
        cb = f"c{int(time.time() * 1000) % 10_000_000_000}"
        qs["callback"] = cb
        qs.setdefault("doc", qs.get("id") or cb)
        qs.setdefault("guest", "false")
        qs["_"] = str(int(time.time() * 1000))
    return urlunparse(parsed._replace(query=urlencode(qs)))


def _es_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _es_zip(data: bytes) -> bool:
    return data[:2] == b"PK"


def _parsear_jsonp(texto: str) -> dict[str, Any]:
    raw = texto.strip()
    m = re.search(r"^[^(]+\((.*)\)\s*;?\s*$", raw, re.S)
    cuerpo = m.group(1) if m else raw
    try:
        data = json.loads(cuerpo)
    except json.JSONDecodeError as e:
        raise DesbloqueoError(
            "jsonp_invalido",
            f"Alfresco no devolvió JSONP reconocible: {raw[:180]}",
            502,
        ) from e
    if not isinstance(data, dict):
        raise DesbloqueoError("jsonp_invalido", "JSONP de Alfresco no es un objeto.", 502)
    return data


def _abs_alfresco(rel: str) -> str:
    if rel.startswith("http://"):
        return "https://" + rel[len("http://"):]
    if rel.startswith("https://"):
        return rel
    return ALFRESCO_BASE + (rel if rel.startswith("/") else "/" + rel)


def descargar_en_memoria(url_descarga: str) -> tuple[bytes, str]:
    """
    Resuelve el ticket de Alfresco y trae el binario a RAM.
    Devuelve (bytes, tipo) donde tipo es 'pdf' o 'zip'.
    """
    session = requests.Session()
    session.headers.update(_headers_alfresco())
    url = _con_callback(url_descarga)
    log.info("GET resolver Alfresco %s", url.split("?")[0])

    try:
        r = session.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        raise DesbloqueoError(
            "alfresco_resolver", f"Fallo al resolver Alfresco: {e}", 502
        ) from e

    payload = r.content
    ctype = (r.headers.get("Content-Type") or "").lower()
    if _es_pdf(payload):
        log.info("Resolver devolvió PDF directo (%s bytes)", len(payload))
        return _limitar_bytes(payload), "pdf"
    if _es_zip(payload):
        log.info("Resolver devolvió ZIP directo (%s bytes)", len(payload))
        return _limitar_bytes(payload), "zip"

    if "json" in ctype or "javascript" in ctype or payload[:1] in (b"{", b"c", b"j"):
        meta = _parsear_jsonp(r.text)
        if str(meta.get("result") or "") not in ("", "200") and not meta.get("downloadUrl"):
            raise DesbloqueoError(
                "alfresco_ticket", f"downloadDoc rechazó el file_code: {meta}", 502
            )
        rel = meta.get("downloadUrl")
        if not rel:
            raise DesbloqueoError(
                "alfresco_ticket", "Alfresco no entregó downloadUrl/alf_ticket.", 502
            )
        file_url = _abs_alfresco(str(rel))
        log.info("GET binario Alfresco (ticket fresco, %s chars)", len(file_url))
        try:
            rf = session.get(file_url, timeout=HTTP_TIMEOUT)
            rf.raise_for_status()
        except requests.RequestException as e:
            raise DesbloqueoError(
                "alfresco_binario", f"Fallo al descargar el binario: {e}", 502
            ) from e
        payload = rf.content
        if _es_pdf(payload):
            return _limitar_bytes(payload), "pdf"
        if _es_zip(payload):
            return _limitar_bytes(payload), "zip"
        raise DesbloqueoError(
            "formato_no_soportado",
            "El archivo de Alfresco no es PDF ni ZIP.",
            422,
        )

    # URL directa (p. ej. PDF de OECE) que no era JSONP.
    raise DesbloqueoError(
        "formato_no_soportado",
        f"La URL no devolvió PDF/ZIP ni JSONP (content-type={ctype or 'desconocido'}).",
        422,
    )


def _limitar_bytes(data: bytes) -> bytes:
    if len(data) > MAX_BYTES:
        raise DesbloqueoError(
            "archivo_grande",
            f"El archivo pesa {len(data)} bytes y supera el tope de {MAX_BYTES}.",
            413,
        )
    return data


def _pdfs_desde_zip(data: bytes) -> list[tuple[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise DesbloqueoError("zip_invalido", f"ZIP corrupto: {e}", 422) from e
    salidas: list[tuple[str, bytes]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        nombre = info.filename
        if not nombre.lower().endswith(".pdf"):
            continue
        salidas.append((nombre, zf.read(info)))
    if not salidas:
        raise DesbloqueoError(
            "zip_sin_pdf", "El ZIP de Alfresco no contiene ningún PDF.", 422
        )
    log.info("ZIP en memoria: %s PDF(s)", len(salidas))
    return salidas


# ---------------------------------------------------------------------------
# 3) Extracción de texto + recorte por regex (~1000 palabras)
# ---------------------------------------------------------------------------
def extraer_paginas_pdf(data: bytes) -> list[tuple[int, str]]:
    """[(nro_pagina 1-based, texto)]. pdfplumber primero; PyMuPDF si falla."""
    paginas = _extraer_pdfplumber(data)
    if paginas and any(t.strip() for _, t in paginas):
        return paginas
    log.warning("pdfplumber vacío o falló — reintento con PyMuPDF")
    paginas = _extraer_pymupdf(data)
    if paginas and any(t.strip() for _, t in paginas):
        return paginas
    raise DesbloqueoError(
        "ocr_vacio",
        "No se pudo extraer texto del PDF (posible escaneo sin capa OCR).",
        500,
    )


def _extraer_pdfplumber(data: bytes) -> list[tuple[int, str]]:
    try:
        import pdfplumber
    except ImportError as e:
        log.warning("pdfplumber no instalado: %s", e)
        return []
    out: list[tuple[int, str]] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                out.append((i, page.extract_text() or ""))
    except Exception as e:
        log.warning("pdfplumber falló: %s", e)
        return []
    return out


def _extraer_pymupdf(data: bytes) -> list[tuple[int, str]]:
    try:
        import fitz
    except ImportError as e:
        raise DesbloqueoError(
            "ocr_deps",
            "Falta PyMuPDF (fitz) para extraer texto del PDF.",
            500,
        ) from e
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise DesbloqueoError("ocr_fitz", f"PyMuPDF no pudo abrir el PDF: {e}", 500) from e
    out: list[tuple[int, str]] = []
    try:
        for i, page in enumerate(doc, start=1):
            out.append((i, page.get_text("text") or ""))
    finally:
        doc.close()
    return out


def recortar_fragmentos(
    paginas: list[tuple[int, str]], max_palabras: int = MAX_PALABRAS
) -> tuple[str, list[int]]:
    """
    Conserva solo páginas con coincidencias de calificación / ISO.
    Recorta a ~max_palabras alrededor del primer match denso.
    """
    hits: list[tuple[int, str]] = []
    for nro, texto in paginas:
        if texto and RE_SECCIONES.search(texto):
            hits.append((nro, texto))
    if not hits:
        log.info("Sin coincidencias regex de ISO/calificación en %s páginas", len(paginas))
        return "", []

    log.info(
        "Regex hit en páginas %s",
        ",".join(str(n) for n, _ in hits),
    )
    palabras: list[str] = []
    usadas: list[int] = []
    for nro, texto in hits:
        usadas.append(nro)
        bloque = _ventana_alrededor_match(texto, max_palabras)
        for tok in bloque.split():
            palabras.append(tok)
            if len(palabras) >= max_palabras:
                return " ".join(palabras[:max_palabras]), usadas
    return " ".join(palabras), usadas


def _ventana_alrededor_match(texto: str, max_palabras: int) -> str:
    m = RE_SECCIONES.search(texto)
    if not m:
        return texto
    tokens = texto.split()
    # aproxima la posición del match en tokens
    prefijo = texto[: m.start()].split()
    centro = len(prefijo)
    radio = max(max_palabras // 2, 80)
    ini = max(0, centro - radio)
    fin = min(len(tokens), centro + radio)
    return " ".join(tokens[ini:fin])


# ---------------------------------------------------------------------------
# 4) LLM → JSON estricto (SDK openai, compatible con agregadores)
# ---------------------------------------------------------------------------
_llm_client = None


def cliente_llm():
    """Cliente único OpenAI-compatible (OpenAI, OpenRouter, KeyAI, …)."""
    global _llm_client
    if _llm_client is not None:
        return _llm_client
    if not LLM_API_KEY:
        raise DesbloqueoError(
            "llm_config",
            "Falta LLM_API_KEY (o OPENAI_API_KEY) en el entorno.",
            500,
        )
    try:
        from openai import OpenAI
    except ImportError as e:
        raise DesbloqueoError(
            "llm_deps", "Falta el paquete openai. pip install openai", 500
        ) from e
    kwargs: dict[str, Any] = {
        "api_key": LLM_API_KEY,
        "timeout": LLM_TIMEOUT,
        "max_retries": LLM_MAX_RETRIES,
    }
    if LLM_BASE_URL:
        kwargs["base_url"] = LLM_BASE_URL
    _llm_client = OpenAI(**kwargs)
    log.info(
        "Cliente LLM listo base_url=%s model=%s retries=%s",
        LLM_BASE_URL or "https://api.openai.com/v1",
        LLM_MODEL,
        LLM_MAX_RETRIES,
    )
    return _llm_client


def analizar_con_llm(fragmento: str, nom_norm: str) -> AnalisisIso:
    user = (
        f"Nomenclatura: {nom_norm}\n\n"
        f"Fragmento de las Bases (secciones de calificación / ISO):\n\n{fragmento}\n\n"
        "Devuelve únicamente el objeto JSON pedido. Sin markdown."
    )
    log.info(
        "Invocando LLM model=%s base_url=%s palabras=%s",
        LLM_MODEL,
        LLM_BASE_URL or "openai-default",
        len(fragmento.split()),
    )
    raw = _completar_chat(user)
    return _parsear_analisis(raw)


def _completar_chat(user: str) -> str:
    """chat.completions.create con JSON mode y mapeo de caídas / rate limits."""
    try:
        from openai import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            InternalServerError,
            PermissionDeniedError,
            RateLimitError,
        )
    except ImportError as e:
        raise DesbloqueoError(
            "llm_deps", "Falta el paquete openai. pip install openai", 500
        ) from e

    client = cliente_llm()
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        )
    except RateLimitError as e:
        raise DesbloqueoError(
            "llm_rate_limit",
            f"El proveedor LLM alcanzó el rate limit. Reintenta más tarde. {e}",
            429,
        ) from e
    except APITimeoutError as e:
        raise DesbloqueoError(
            "llm_timeout",
            f"El proveedor LLM no respondió a tiempo ({LLM_TIMEOUT}s). {e}",
            504,
        ) from e
    except APIConnectionError as e:
        raise DesbloqueoError(
            "llm_conexion",
            f"No se pudo conectar con el proveedor LLM ({LLM_BASE_URL or 'api.openai.com'}). {e}",
            502,
        ) from e
    except AuthenticationError as e:
        raise DesbloqueoError(
            "llm_auth",
            f"LLM_API_KEY rechazada por el proveedor. {e}",
            500,
        ) from e
    except PermissionDeniedError as e:
        raise DesbloqueoError(
            "llm_permiso",
            f"El proveedor denegó el acceso al modelo {LLM_MODEL}. {e}",
            502,
        ) from e
    except BadRequestError as e:
        raise DesbloqueoError(
            "llm_request",
            f"El proveedor rechazó la petición (modelo o JSON mode). {e}",
            502,
        ) from e
    except InternalServerError as e:
        raise DesbloqueoError(
            "llm_caida",
            f"El proveedor LLM está caído o inestable (5xx). {e}",
            502,
        ) from e
    except APIStatusError as e:
        codigo = getattr(e, "status_code", None)
        if codigo == 429:
            raise DesbloqueoError(
                "llm_rate_limit",
                f"Rate limit del proveedor (HTTP 429). {e}",
                429,
            ) from e
        if codigo in (502, 503, 504):
            raise DesbloqueoError(
                "llm_caida",
                f"Proveedor LLM no disponible (HTTP {codigo}). {e}",
                502,
            ) from e
        raise DesbloqueoError(
            "llm_error",
            f"Error HTTP {codigo} del proveedor LLM. {e}",
            502,
        ) from e
    except DesbloqueoError:
        raise
    except Exception as e:
        raise DesbloqueoError("llm_error", f"Fallo inesperado del LLM: {e}", 500) from e

    if not resp.choices:
        raise DesbloqueoError("llm_vacio", "El proveedor no devolvió choices.", 502)
    contenido = (resp.choices[0].message.content or "").strip()
    if not contenido:
        raise DesbloqueoError("llm_vacio", "El LLM devolvió una respuesta vacía.", 502)
    return contenido


def _parsear_analisis(raw: str) -> AnalisisIso:
    texto = raw.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*", "", texto)
        texto = re.sub(r"\s*```$", "", texto)
    try:
        data = json.loads(texto)
    except json.JSONDecodeError as e:
        raise DesbloqueoError(
            "llm_json", f"El LLM no devolvió JSON válido: {raw[:300]}", 500
        ) from e

    condicion = str(data.get("condicion") or "No aplica").strip()
    mapa = {
        "obligatorio": "Obligatorio",
        "requisito": "Obligatorio",
        "puntaje": "Puntaje",
        "opcional": "Puntaje",
        "no aplica": "No aplica",
        "noaplica": "No aplica",
        "ninguno": "No aplica",
    }
    condicion = mapa.get(condicion.lower(), condicion)
    if condicion not in ("Obligatorio", "Puntaje", "No aplica"):
        condicion = "No aplica"

    normas: list[str] = []
    for item in data.get("normas_identificadas") or []:
        n = re.sub(r"\s+", " ", str(item).strip().upper())
        if n and n not in normas:
            normas.append(n)

    condicion_ok: Literal["Obligatorio", "Puntaje", "No aplica"] = (
        condicion if condicion in ("Obligatorio", "Puntaje", "No aplica") else "No aplica"
    )
    return AnalisisIso(
        requiere_iso=bool(data.get("requiere_iso")),
        normas_identificadas=normas,
        condicion=condicion_ok,
        resumen_requisito=str(
            data.get("resumen_requisito") or "No se identificó exigencia ISO."
        ).strip(),
    )


def analisis_sin_hits() -> AnalisisIso:
    return AnalisisIso(
        requiere_iso=False,
        normas_identificadas=[],
        condicion="No aplica",
        resumen_requisito=(
            "No se hallaron secciones de calificación, certificaciones "
            "o normas ISO en el documento prioritario."
        ),
    )


# ---------------------------------------------------------------------------
# 5) Persistencia
# ---------------------------------------------------------------------------
def persistir_requiere_iso(nom_norm: str, payload: dict[str, Any]) -> None:
    client = get_client()
    try:
        client.table("convocatorias").update({
            "requiere_iso": json.dumps(payload, ensure_ascii=False),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("nomenclatura_norm", nom_norm).execute()
    except Exception as e:
        raise DesbloqueoError(
            "supabase_update",
            f"No se pudo actualizar convocatorias.requiere_iso: {e}",
            502,
        ) from e
    log.info("requiere_iso actualizado para %s", nom_norm)


# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------
def ejecutar_analisis(nom_norm: str) -> AnalizarIsoResponse:
    doc = buscar_documento_prioritario(nom_norm)
    binario, tipo = descargar_en_memoria(doc.url_descarga)
    log.info(
        "Binario en RAM tipo=%s bytes=%s nom=%s",
        tipo, len(binario), nom_norm,
    )

    if tipo == "zip":
        pdfs = _pdfs_desde_zip(binario)
        mejor_frag, mejor_pags, mejor_paginas_doc = "", [], []
        for nombre, pdf_bytes in pdfs:
            try:
                pags = extraer_paginas_pdf(pdf_bytes)
            except DesbloqueoError as e:
                log.warning("PDF %s del ZIP omitido: %s", nombre, e.mensaje)
                continue
            frag, usadas = recortar_fragmentos(pags)
            if len(frag.split()) > len(mejor_frag.split()):
                mejor_frag, mejor_pags, mejor_paginas_doc = frag, usadas, pags
                log.info("ZIP: mejor candidato %s (%s palabras)", nombre, len(frag.split()))
        if not mejor_paginas_doc and not mejor_frag:
            raise DesbloqueoError(
                "ocr_vacio",
                "Ningún PDF del ZIP tuvo texto extraíble.",
                500,
            )
        fragmento, paginas_ok = mejor_frag, mejor_pags
    else:
        paginas = extraer_paginas_pdf(binario)
        log.info("PDF páginas=%s nom=%s", len(paginas), nom_norm)
        fragmento, paginas_ok = recortar_fragmentos(paginas)

    if fragmento:
        analisis = analizar_con_llm(fragmento, nom_norm)
        llm_ok = True
    else:
        analisis = analisis_sin_hits()
        llm_ok = False
        log.info("Sin fragmento ISO — se omite el LLM para ahorrar costo")

    persistido = {
        **analisis.model_dump(),
        "documento": doc.model_dump(),
        "paginas_analizadas": paginas_ok,
        "analizado_at": datetime.now(timezone.utc).isoformat(),
    }
    persistir_requiere_iso(nom_norm, persistido)

    return AnalizarIsoResponse(
        nomenclatura_norm=nom_norm,
        documento=doc,
        analisis=analisis,
        paginas_analizadas=paginas_ok,
        palabras_enviadas=len(fragmento.split()) if fragmento else 0,
        llm_invocado=llm_ok,
    )


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(
    title="LicitApp Desbloqueo ISO",
    version="1.0.0",
    description=(
        "Analiza on-demand las Bases de una convocatoria y determina "
        "si el Estado exige certificación ISO."
    ),
)


@app.exception_handler(DesbloqueoError)
async def _handle_desbloqueo(_, exc: DesbloqueoError) -> JSONResponse:
    log.error("DesbloqueoError %s: %s", exc.codigo, exc.mensaje)
    return JSONResponse(
        status_code=exc.status,
        content=ErrorBody(detail=exc.mensaje, codigo=exc.codigo).model_dump(),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "servicio": "desbloqueo-iso"}


@app.post(
    "/api/v1/analizar-iso",
    response_model=AnalizarIsoResponse,
    responses={
        404: {"model": ErrorBody},
        422: {"model": ErrorBody},
        429: {"model": ErrorBody},
        500: {"model": ErrorBody},
        502: {"model": ErrorBody},
        504: {"model": ErrorBody},
    },
)
def analizar_iso(body: AnalizarIsoRequest) -> AnalizarIsoResponse:
    """
    Gasta el crédito del usuario: descarga el documento prioritario en memoria,
    recorta las secciones ISO y actualiza convocatorias.requiere_iso.
    """
    log.info("POST /api/v1/analizar-iso nomenclatura_norm=%s", body.nomenclatura_norm)
    try:
        return ejecutar_analisis(body.nomenclatura_norm)
    except DesbloqueoError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Error no controlado en analizar-iso")
        raise DesbloqueoError("interno", f"Error interno: {e}", 500) from e


if __name__ == "__main__":
    import uvicorn

    host = _env("DESBLOQUEO_HOST") or "0.0.0.0"
    port = _env_int("DESBLOQUEO_PORT", 8080)
    uvicorn.run("api_desbloqueo:app", host=host, port=port, reload=False)
