"""
Cliente Supabase: upsert a convocatorias / documentos_proceso / items_proceso /
proveedores.

Estrategia on-demand: aquí NUNCA se descarga un binario. De cada documento se
persiste el file_code y la URL de descarga de Alfresco; el PDF/ZIP se resuelve
asíncronamente cuando el usuario desbloquea la licitación en el frontend.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

try:
    from lib_nomenclatura import extraer_nomenclatura, normalizar_nomenclatura
except ImportError:
    def normalizar_nomenclatura(valor):
        import re
        if valor is None:
            return ""
        return re.sub(r"\s+", "", str(valor).upper().strip())

    def extraer_nomenclatura(*candidatos):
        for raw in candidatos:
            if not raw:
                continue
            n = normalizar_nomenclatura(raw)
            if len(n) >= 8:
                return str(raw).strip(), n
        return "", ""


def _load_env_file(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = "=" if "=" in line else (":" if ":" in line else None)
        if not sep:
            continue
        k, v = line.split(sep, 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_env():
    here = Path(__file__).resolve().parent
    _load_env_file(here / ".env")


load_env()

BATCH = 200

# Estado inicial del análisis IA: la ficha nace bloqueada y se procesa on-demand.
ESTADO_ISO_BLOQUEADO = "Bloqueado"


def _alfresco_base():
    return (
        os.environ.get("SEACE_ALFRESCO") or "https://alfprod.seace.gob.pe/alfresco"
    ).strip().rstrip("/")


def construir_url_alfresco(file_code):
    """
    URL dinámica de descarga (no se invoca aquí). Es el resolver JSONP de
    Alfresco: devuelve un downloadUrl con alf_ticket fresco, así que la URL
    guardada no caduca y sirve para el desbloqueo posterior.
    """
    if not file_code:
        return ""
    return (
        f"{_alfresco_base()}/service/osce/downloadDoc"
        f"?id={file_code}&doc={file_code}&guest=false"
    )


def get_client():
    load_env()
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (
        os.environ.get("SUPABASE_SECRET_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or ""
    ).strip()
    if not url or not key:
        raise RuntimeError("Faltan SUPABASE_URL y/o SUPABASE_SECRET_KEY en .env")
    from supabase import create_client

    return create_client(url, key)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _chunks(rows, n=BATCH):
    for i in range(0, len(rows), n):
        yield rows[i : i + n]


def _fecha_iso(valor):
    if not valor:
        return None
    s = str(valor).strip()
    for fmt, cut in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%d/%m/%Y %H:%M", 16),
        ("%Y-%m-%d", 10),
        ("%d/%m/%Y", 10),
    ):
        try:
            return datetime.strptime(s[:cut], fmt).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def upsert_rows(table, rows, on_conflict):
    """Bulk upsert. Ante conflicto de unicidad reintenta fila a fila (no falla el lote)."""
    if not rows:
        return 0
    client = get_client()
    ok = 0
    for chunk in _chunks(rows):
        try:
            client.table(table).upsert(chunk, on_conflict=on_conflict).execute()
            ok += len(chunk)
        except Exception as e:
            print(f"[!] Upsert lote {table} ({len(chunk)}): {e} → reintento 1x1")
            for row in chunk:
                try:
                    client.table(table).upsert(row, on_conflict=on_conflict).execute()
                    ok += 1
                except Exception as e2:
                    print(f"[!] Fila {table} omitida: {e2}")
    print(f"[+] Supabase {table}: {ok}/{len(rows)} upserts")
    return ok


def convocatoria_from_oece_lead(lead):
    nom_raw = lead.get("Nomenclatura") or ""
    nom_norm = lead.get("Nomenclatura Norma") or normalizar_nomenclatura(nom_raw)
    if not nom_norm:
        _, nom_norm = extraer_nomenclatura(
            lead.get("Licitación"), lead.get("OCID"), nom_raw
        )
    if not nom_norm:
        return None
    return {
        "nomenclatura_norm": nom_norm,
        "nomenclatura": nom_raw or nom_norm,
        "entidad": lead.get("Entidad Convocante") or "",
        "fecha_publicacion": _fecha_iso(lead.get("Fecha Convocatoria")),
        "objeto": lead.get("Objeto de Contratación") or lead.get("Categoría") or "",
        "descripcion": lead.get("Objeto / Descripción") or lead.get("Licitación") or "",
        "monto": str(lead.get("Monto Referencial (S/.)") or ""),
        "moneda": "PEN",
        "fuente": lead.get("Fuente") or "oece",
        "ocid": lead.get("OCID") or "",
        "ficha_url": lead.get("Ver Proceso (Portal OECE)") or "",
        "url_bases": lead.get("URL Bases (PDF Original)") or "",
        "file_code": None,
        "nid_convocatoria": None,
        "nid_proceso": None,
        "updated_at": _now(),
    }


def proveedor_from_oece_lead(lead):
    ruc = str(lead.get("RUC") or "").strip()
    nom_norm = lead.get("Nomenclatura Norma") or normalizar_nomenclatura(lead.get("Nomenclatura"))
    if not ruc or not nom_norm:
        return None
    return {
        "ruc": ruc,
        "nomenclatura_norm": nom_norm,
        "razon_social": lead.get("Razón Social") or "",
        "condicion": lead.get("Condición") or "",
        "telefono": lead.get("Teléfono / Celular (RNP)") or "",
        "email": lead.get("Email de Contacto (RNP)") or "",
        "updated_at": _now(),
    }


def upsert_oece_leads(leads):
    convs, seen = [], set()
    provs = []
    for lead in leads:
        c = convocatoria_from_oece_lead(lead)
        if c and c["nomenclatura_norm"] not in seen:
            seen.add(c["nomenclatura_norm"])
            convs.append(c)
        p = proveedor_from_oece_lead(lead)
        if p:
            provs.append(p)
    upsert_rows("convocatorias", convs, "nomenclatura_norm")
    upsert_rows("proveedores", provs, "ruc,nomenclatura_norm")
    return len(convs), len(provs)


def upsert_seace_fila(fila, extra=None):
    extra = extra or {}
    nom = fila.get("nomenclatura") or ""
    nom_norm = fila.get("nomenclatura_norm") or normalizar_nomenclatura(nom)
    if not nom_norm:
        return None
    row = {
        "nomenclatura_norm": nom_norm,
        "nomenclatura": nom,
        "entidad": fila.get("entidad") or "",
        "fecha_publicacion": _fecha_iso(fila.get("fecha_publicacion")),
        "objeto": fila.get("objeto") or "",
        "descripcion": fila.get("descripcion") or "",
        "monto": str(fila.get("vr_ve_cuantia") or ""),
        "moneda": fila.get("moneda") or "",
        "fuente": fila.get("fuente") or "seace",
        "ocid": None,
        "ficha_url": extra.get("ficha_url") or fila.get("ficha_url") or "",
        "url_bases": extra.get("url_bases") or fila.get("url_bases") or "",
        "file_code": extra.get("file_code") or fila.get("file_code") or "",
        "nid_convocatoria": fila.get("nid_convocatoria"),
        "nid_proceso": fila.get("nid_proceso"),
        "updated_at": _now(),
    }
    upsert_rows("convocatorias", [row], "nomenclatura_norm")
    return nom_norm


def documento_row(nom_norm, doc):
    """Fila on-demand: file_code + URL Alfresco. Nunca ruta local ni bytes."""
    file_id = doc.get("file_id") or doc.get("file_code")
    if not file_id or not nom_norm:
        return None
    file_code = doc.get("file_code") or file_id
    return {
        "file_id": file_id,
        "nomenclatura_norm": nom_norm,
        "categoria": doc.get("categoria") or "",
        "documento": doc.get("documento") or "",
        "nombre_archivo": doc.get("nombre_archivo") or "",
        "file_code": file_code,
        "url_descarga": doc.get("url_descarga") or construir_url_alfresco(file_code),
        "updated_at": _now(),
    }


def upsert_documentos(nom_norm, docs):
    """Registra todos los documentos de una ficha sin descargar ningún binario."""
    rows = []
    vistos = set()
    for doc in docs or []:
        row = documento_row(nom_norm, doc)
        if not row or row["file_id"] in vistos:
            continue
        vistos.add(row["file_id"])
        rows.append(row)
    return upsert_rows("documentos_proceso", rows, "file_id")


def nomenclaturas_en_nube(fuente=None):
    """Set de nomenclaturas normalizadas ya cargadas (llave de deduplicación)."""
    client = get_client()
    known = set()
    start = 0
    while True:
        q = client.table("convocatorias").select("nomenclatura_norm")
        if fuente:
            q = q.eq("fuente", fuente)
        res = q.range(start, start + 999).execute()
        data = res.data or []
        for row in data:
            if row.get("nomenclatura_norm"):
                known.add(row["nomenclatura_norm"])
        if len(data) < 1000:
            break
        start += 1000
    return known


def contar_convocatorias(fuente=None):
    client = get_client()
    q = client.table("convocatorias").select("nomenclatura_norm", count="exact")
    if fuente:
        q = q.eq("fuente", fuente)
    res = q.limit(1).execute()
    return res.count or 0


# ---------------------------------------------------------------------------
# PROD6 — Compras Menores (<= 8 UIT), API REST pública sin CAPTCHA
# ---------------------------------------------------------------------------
def convocatoria_from_prod6(cab, resumen=None):
    """
    Mapea uitContratoCompletoProjection (+ fila del buscador) a convocatorias.
    La llave sigue siendo nomenclatura_norm (nroDescripcion normalizado).
    """
    resumen = resumen or {}
    nom_raw = (cab.get("nroDescripcion") or resumen.get("desContratacion") or "").strip()
    nom_norm = normalizar_nomenclatura(nom_raw)
    if not nom_norm:
        return None
    id_contrato = cab.get("idContrato") or resumen.get("idContrato")
    monto = (
        cab.get("montoContrato")
        if cab.get("montoContrato") is not None
        else resumen.get("montoContrato")
    )
    # requiere_iso se omite a propósito: lo pone el DEFAULT en el INSERT y así
    # un re-upsert no pisa el estado de desbloqueo del usuario.
    return {
        "nomenclatura_norm": nom_norm,
        "nomenclatura": nom_raw,
        "entidad": cab.get("nomEntidad") or resumen.get("nomEntidad") or "",
        "fecha_publicacion": _fecha_iso(
            cab.get("fecPublica") or resumen.get("fecPublica")
        ),
        "objeto": cab.get("nomObjetoContrato") or resumen.get("nomObjetoContrato") or "",
        "descripcion": (
            cab.get("desObjetoContrato") or resumen.get("desObjetoContrato") or ""
        ),
        "monto": "" if monto is None else str(monto),
        "moneda": "PEN",
        "fuente": "PROD6",
        "estado": cab.get("nomEstadoContrato") or resumen.get("nomEstadoContrato") or "",
        "id_contrato": None if id_contrato is None else str(id_contrato),
        "fecha_fin_cotizacion": _fecha_iso(resumen.get("fecFinCotizacion")),
        "ficha_url": (
            f"https://prod6.seace.gob.pe/compras-menores/detalle/{id_contrato}"
            if id_contrato
            else ""
        ),
        "url_bases": "",
        "file_code": None,
        "updated_at": _now(),
    }


def items_from_prod6(nom_norm, items):
    filas = []
    for i, it in enumerate(items or [], start=1):
        filas.append({
            "nomenclatura_norm": nom_norm,
            "secuencia": i,
            "id_contrato_item": it.get("idContratoItem"),
            "codigo_cubso": it.get("codCubso") or "",
            "nombre_cubso": it.get("nomCubso") or "",
            "descripcion_item": it.get("descripcionItem") or "",
            "cantidad": it.get("cantidad"),
            "unidad_medida": it.get("nomUnidadMedida") or "",
            "distrito": it.get("nomDistritoExt") or it.get("nomDistrito") or "",
            "moneda": it.get("nomMoneda") or "",
            "precio_total": it.get("precioTotal"),
            "updated_at": _now(),
        })
    return filas


def upsert_prod6_proceso(cab, resumen=None, items=None):
    """Inserta una compra menor + sus ítems. Devuelve nomenclatura_norm o None."""
    conv = convocatoria_from_prod6(cab, resumen)
    if not conv:
        return None
    if not upsert_rows("convocatorias", [conv], "nomenclatura_norm"):
        return None
    filas = items_from_prod6(conv["nomenclatura_norm"], items)
    if filas:
        upsert_rows("items_proceso", filas, "nomenclatura_norm,secuencia")
    return conv["nomenclatura_norm"]


def upsert_prod6_lote(procesos):
    """
    procesos: lista de dicts {cab, resumen, items}. Hace el upsert en bloque
    (convocatorias primero por la FK de items_proceso).
    """
    convs, items, vistos = [], [], set()
    for p in procesos:
        conv = convocatoria_from_prod6(p.get("cab") or {}, p.get("resumen"))
        if not conv or conv["nomenclatura_norm"] in vistos:
            continue
        vistos.add(conv["nomenclatura_norm"])
        convs.append(conv)
        items.extend(items_from_prod6(conv["nomenclatura_norm"], p.get("items")))
    ok = upsert_rows("convocatorias", convs, "nomenclatura_norm")
    if items:
        upsert_rows("items_proceso", items, "nomenclatura_norm,secuencia")
    return ok, len(items)
