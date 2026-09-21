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
from lib_embudo import es_estado_terminal
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
    row = {
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
        "tipo_procedimiento": lead.get("Tipo Procedimiento") or "",
        "categoria": lead.get("Categoría") or lead.get("Categoria") or "",
        "estado": lead.get("Estado") or "",
        "updated_at": _now(),
    }
    if lead.get("Bloqueada") is True or lead.get("Bloqueada") in ("1", "true", "True"):
        row["bloqueada"] = True
    elif es_estado_terminal(row.get("estado")):
        row["bloqueada"] = True
    return row


def proveedor_from_oece_lead(lead, ficha=None):
    ruc = str(lead.get("RUC") or "").replace("PE-RUC-", "").strip()
    nom_norm = lead.get("Nomenclatura Norma") or normalizar_nomenclatura(lead.get("Nomenclatura"))
    if not ruc or not nom_norm:
        return None
    ficha = ficha or {}
    tel = (
        ficha.get("telefono_rnp")
        or ficha.get("telefono")
        or lead.get("Teléfono / Celular (RNP)")
        or ""
    )
    mail = (
        ficha.get("email_rnp")
        or ficha.get("email")
        or lead.get("Email de Contacto (RNP)")
        or ""
    )
    return {
        "ruc": ruc,
        "nomenclatura_norm": nom_norm,
        "razon_social": lead.get("Razón Social") or "",
        "condicion": lead.get("Condición") or "",
        "telefono": tel,
        "email": mail,
        "telefono_rnp": tel,
        "email_rnp": mail,
        "departamento": ficha.get("departamento") or lead.get("Departamento") or "",
        "provincia": ficha.get("provincia") or lead.get("Provincia") or "",
        "distrito": ficha.get("distrito") or lead.get("Distrito") or "",
        "estado_sunat": (
            ficha.get("estado_sunat") or lead.get("Estado SUNAT") or "N/D"
        ),
        "condicion_domicilio": (
            ficha.get("condicion_domicilio")
            or lead.get("Condición Domicilio")
            or "N/D"
        ),
        "habilitado_rnp": (
            ficha.get("habilitado_rnp") or lead.get("Habilitado RNP") or "No"
        ),
        "apto_contratar": (
            ficha.get("apto_contratar") or lead.get("Apto para Contratar") or "No"
        ),
        "ficha_rnp_url": ficha.get("ficha_rnp_url") or lead.get("Ficha RNP (Web)") or "",
        "updated_at": _now(),
    }


def upsert_oece_leads(leads, enriquecer=True):
    """
    Upsert OECE. Si enriquecer=True (default) consulta RNP+SUNAT por RUC
    único ANTES de insertar en proveedores: estado_sunat, condicion_rnp
    (habilitado/apto) y teléfonos/emails.
    """
    convs, seen = [], set()
    fichas = {}
    if enriquecer and os.environ.get("ENRIQUECER_PROVEEDORES", "1").lower() not in (
        "0", "false", "no",
    ):
        try:
            from enriquecer_proveedor import enriquecer_rucs

            fichas = enriquecer_rucs(l.get("RUC") for l in leads)
        except Exception as e:
            print(f"[!] Enriquecimiento RNP/SUNAT falló, se inserta ficha vacía: {e}")

    provs = []
    for lead in leads:
        c = convocatoria_from_oece_lead(lead)
        if c and c["nomenclatura_norm"] not in seen:
            seen.add(c["nomenclatura_norm"])
            convs.append(c)
        ruc = str(lead.get("RUC") or "").replace("PE-RUC-", "").strip()
        p = proveedor_from_oece_lead(lead, fichas.get(ruc))
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
    if es_estado_terminal(fila.get("estado") or extra.get("estado")):
        row["bloqueada"] = True
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


def nomenclaturas_bloqueadas():
    """Nomenclaturas que OECE marcó terminales: PROD2/PROD6 no las tocan."""
    client = get_client()
    known = set()
    start = 0
    while True:
        res = (
            client.table("convocatorias")
            .select("nomenclatura_norm")
            .eq("bloqueada", True)
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
# Hard whitelist: columnas base + campos comerciales (cronograma denormalizado).
# Cualquier llave fuera de esta tupla se descarta antes del upsert (PGRST204).
COLUMNAS_CONVOCATORIAS_VALIDAS = (
    "nomenclatura_norm",
    "nomenclatura",
    "entidad",
    "fecha_publicacion",
    "objeto",
    "descripcion",
    "monto",
    "moneda",
    "fuente",
    "ocid",
    "categoria",
    "bloqueada",
    "estado",
    "id_contrato",
    "fecha_fin_cotizacion",
    "tipo_procedimiento",
    "fecha_inicio_consultas",
    "fecha_fin_consultas",
    "fecha_inicio_cotizacion",
    "fecha_integracion",
    "fecha_presentacion",
    "ficha_url",
    "url_bases",
    "file_code",
    "updated_at",
)


def _sanitizar_convocatoria(convocatoria_dict):
    """Hard whitelist: el payload solo conserva columnas válidas de convocatorias."""
    if not convocatoria_dict:
        return convocatoria_dict
    return {
        k: v
        for k, v in convocatoria_dict.items()
        if k in COLUMNAS_CONVOCATORIAS_VALIDAS
    }


# idEtapaContrato en uitContratoEtapaProjectionList (JS del buscador público).
PROD6_ETAPA_CONSULTAS = 1
PROD6_ETAPA_COTIZACION = 2
PROD6_ETAPA_INTEGRACION = 3
PROD6_ETAPA_PRESENTACION = 4
PROD6_ARCHIVO_BASE = (
    "https://prod6.seace.gob.pe/v1/s8uit-services/archivo"
    "/archivos-publico/descargar-archivo-contrato"
)


def _etapa_por_id(etapas, id_etapa):
    for et in etapas or []:
        if int(et.get("idEtapaContrato") or 0) == id_etapa:
            return et
    return {}


def _etapa_por_nombre(etapas, *needles):
    for et in etapas or []:
        nom = (et.get("nomEtapaContrato") or "").lower()
        if any(n in nom for n in needles):
            return et
    return {}


def convocatoria_from_prod6(cab, resumen=None, etapas=None, docs=None):
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
    consulta = _etapa_por_id(etapas, PROD6_ETAPA_CONSULTAS)
    cotiza = _etapa_por_id(etapas, PROD6_ETAPA_COTIZACION)
    integra = (
        _etapa_por_id(etapas, PROD6_ETAPA_INTEGRACION)
        or _etapa_por_nombre(etapas, "integrac")
    )
    presenta = (
        _etapa_por_id(etapas, PROD6_ETAPA_PRESENTACION)
        or _etapa_por_nombre(etapas, "present")
    )
    url_bases = ""
    file_code = None
    if docs:
        primero = docs[0]
        url_bases = primero.get("url_descarga") or ""
        file_code = primero.get("file_code")
    # requiere_iso se omite a propósito: lo pone el DEFAULT en el INSERT y así
    # un re-upsert no pisa el estado de desbloqueo del usuario.
    row = {
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
        "ocid": None,
        "categoria": cab.get("nomObjetoContrato") or resumen.get("nomObjetoContrato") or "",
        "estado": cab.get("nomEstadoContrato") or resumen.get("nomEstadoContrato") or "",
        "id_contrato": None if id_contrato is None else str(id_contrato),
        "tipo_procedimiento": "Contratación menor (≤ 8 UIT)",
        "fecha_inicio_consultas": _fecha_iso(consulta.get("fecIni")),
        "fecha_fin_consultas": _fecha_iso(consulta.get("fecFin")),
        "fecha_inicio_cotizacion": _fecha_iso(cotiza.get("fecIni")),
        "fecha_fin_cotizacion": _fecha_iso(
            cotiza.get("fecFin") or resumen.get("fecFinCotizacion")
        ),
        "fecha_integracion": _fecha_iso(
            integra.get("fecFin") or integra.get("fecIni")
        ),
        "fecha_presentacion": _fecha_iso(
            presenta.get("fecFin") or presenta.get("fecIni")
        ),
        "ficha_url": (
            f"https://prod6.seace.gob.pe/buscador-publico/contrataciones/{id_contrato}"
            if id_contrato
            else ""
        ),
        "url_bases": url_bases,
        "file_code": file_code,
        "nid_convocatoria": None,
        "nid_proceso": None,
        "updated_at": _now(),
    }
    if es_estado_terminal(row["estado"]):
        row["bloqueada"] = True
    return _sanitizar_convocatoria(row)


def cronograma_from_prod6(nom_norm, etapas):
    filas = []
    for et in etapas or []:
        id_etapa = et.get("idEtapaContrato")
        if id_etapa is None:
            continue
        filas.append({
            "nomenclatura_norm": nom_norm,
            "id_etapa": int(id_etapa),
            "nombre_etapa": et.get("nomEtapaContrato") or "",
            "fecha_inicio": _fecha_iso(et.get("fecIni")),
            "fecha_fin": _fecha_iso(et.get("fecFin")),
            "updated_at": _now(),
        })
    return filas


def documentos_from_prod6(nom_norm, docs):
    filas = []
    for doc in docs or []:
        file_id = doc.get("file_id") or doc.get("file_code")
        if not file_id:
            continue
        filas.append({
            "file_id": str(file_id),
            "nomenclatura_norm": nom_norm,
            "categoria": doc.get("categoria") or "requerimiento",
            "documento": doc.get("documento") or "Requerimiento / Bases",
            "nombre_archivo": doc.get("nombre_archivo") or "",
            "file_code": str(doc.get("file_code") or file_id),
            "url_descarga": doc.get("url_descarga") or (
                f"{PROD6_ARCHIVO_BASE}/{file_id}"
            ),
            "updated_at": _now(),
        })
    return filas


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


def _upsert_padres_convocatorias(convs):
    """
    Upsert de convocatorias con el mismo reintento 1x1 que upsert_rows,
    devolviendo las nomenclatura_norm que sí quedaron en la tabla padre.
    """
    if not convs:
        return set()
    convs = [_sanitizar_convocatoria(c) for c in convs]
    client = get_client()
    landed = set()
    for chunk in _chunks(convs):
        try:
            client.table("convocatorias").upsert(
                chunk, on_conflict="nomenclatura_norm"
            ).execute()
            landed.update(c["nomenclatura_norm"] for c in chunk)
        except Exception as e:
            print(f"[!] Upsert lote convocatorias ({len(chunk)}): {e} → reintento 1x1")
            for row in chunk:
                try:
                    client.table("convocatorias").upsert(
                        row, on_conflict="nomenclatura_norm"
                    ).execute()
                    landed.add(row["nomenclatura_norm"])
                except Exception as e2:
                    print(f"[!] Fila convocatorias omitida: {e2}")
    print(f"[+] Supabase convocatorias: {len(landed)}/{len(convs)} upserts")
    return landed


def upsert_prod6_proceso(cab, resumen=None, items=None, etapas=None, docs=None):
    """Inserta una compra menor + ítems + docs. Devuelve nomenclatura_norm."""
    conv = convocatoria_from_prod6(cab, resumen, etapas=etapas, docs=docs)
    if not conv:
        return None
    if not upsert_rows("convocatorias", [conv], "nomenclatura_norm"):
        return None
    nom = conv["nomenclatura_norm"]
    filas = items_from_prod6(nom, items)
    if filas:
        upsert_rows("items_proceso", filas, "nomenclatura_norm,secuencia")
    # cronograma_proceso no existe aún en este proyecto de Supabase.
    # crono = cronograma_from_prod6(nom, etapas)
    # if crono:
    #     upsert_rows("cronograma_proceso", crono, "nomenclatura_norm,id_etapa")
    docs_rows = documentos_from_prod6(nom, docs)
    if docs_rows:
        upsert_rows("documentos_proceso", docs_rows, "file_id")
    return nom


def upsert_prod6_lote(procesos):
    """
    procesos: lista de dicts {cab, resumen, items, etapas, docs}.
    Upsert en bloque: convocatorias primero (y con éxito) por las FK.
    Ítems y documentos solo para nomenclaturas que sí persistieron en el padre.
    cronograma_proceso se omite: la tabla no existe aún en este Supabase.
    """
    convs, items_por_nom, docs_por_nom, vistos = [], {}, {}, set()
    for p in procesos:
        conv = convocatoria_from_prod6(
            p.get("cab") or {},
            p.get("resumen"),
            etapas=p.get("etapas"),
            docs=p.get("docs"),
        )
        if not conv or conv["nomenclatura_norm"] in vistos:
            continue
        vistos.add(conv["nomenclatura_norm"])
        nom = conv["nomenclatura_norm"]
        convs.append(conv)
        items_por_nom[nom] = items_from_prod6(nom, p.get("items"))
        docs_por_nom[nom] = documentos_from_prod6(nom, p.get("docs"))
        # cronograma_proceso no existe aún — no se arma ni se upserta.
        # cronos.extend(cronograma_from_prod6(nom, p.get("etapas")))

    padres_ok = _upsert_padres_convocatorias(convs)
    items = [
        row for nom in padres_ok for row in items_por_nom.get(nom, [])
    ]
    docs_all = [
        row for nom in padres_ok for row in docs_por_nom.get(nom, [])
    ]
    omitidas = vistos - padres_ok
    if omitidas:
        print(
            f"[!] PROD6: {len(omitidas)} nomenclaturas sin fila en convocatorias; "
            "se omiten items_proceso y documentos_proceso de esas llaves"
        )
    if items:
        upsert_rows("items_proceso", items, "nomenclatura_norm,secuencia")
    # if cronos:
    #     upsert_rows("cronograma_proceso", cronos, "nomenclatura_norm,id_etapa")
    if docs_all:
        upsert_rows("documentos_proceso", docs_all, "file_id")
    return len(padres_ok), len(items)
