"""Cliente Supabase: upsert a convocatorias / documentos_proceso / proveedores."""
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
    _load_env_file(Path(r"C:\scraper_licitigo\scraper_seace_pasante_proyecto\.env"))
    _load_env_file(Path(r"C:\extraccion_oesce\pipeline\.env"))


load_env()

BATCH = 200


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


def upsert_documento(nom_norm, doc, url_descarga="", ruta_local="", nbytes=None):
    file_id = doc.get("file_id") or doc.get("file_code")
    if not file_id or not nom_norm:
        return
    upsert_rows(
        "documentos_proceso",
        [{
            "file_id": file_id,
            "nomenclatura_norm": nom_norm,
            "categoria": doc.get("categoria") or "",
            "documento": doc.get("documento") or "",
            "nombre_archivo": doc.get("nombre_archivo") or "",
            "url_descarga": url_descarga or "",
            "ruta_local": str(ruta_local or ""),
            "bytes": nbytes,
            "updated_at": _now(),
        }],
        "file_id",
    )


def nomenclaturas_en_nube():
    client = get_client()
    known = set()
    start = 0
    while True:
        res = (
            client.table("convocatorias")
            .select("nomenclatura_norm")
            .range(start, start + 999)
            .execute()
        )
        data = res.data or []
        for row in data:
            if row.get("nomenclatura_norm"):
                known.add(row["nomenclatura_norm"])
        if len(data) < 1000:
            break
        start += 1000
    return known
