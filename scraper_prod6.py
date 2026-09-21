"""
SEACE PROD6 (Compras Menores <= 8 UIT) — API REST JSON pública, sin CAPTCHA.

Flujo:
  A) Búsqueda paginada  → /contrataciones/buscador
  B) Deduplicación      → nroDescripcion normalizado vs. Supabase
  C) Detalle            → /contrataciones/listar-completo?id_contrato={id}
  D) Upsert             → convocatorias (fuente='PROD6') + items_proceso

No descarga binarios: igual que PROD2, este módulo solo persiste metadatos.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from lib_embudo import guardar_radar, horas_radar_default
from lib_nomenclatura import normalizar_nomenclatura
from proxy_iproyal import aplicar_proxy, log_proxy_status
from supabase_sync import (
    load_env,
    nomenclaturas_bloqueadas,
    nomenclaturas_en_nube,
    upsert_prod6_lote,
)

LOTE_UPSERT = 100

load_env()


def _env(name, default=""):
    val = os.environ.get(name)
    return default if val is None else val.strip()


def _env_int(name, default=None):
    raw = _env(name, "")
    return default if raw == "" else int(raw)


def _env_float(name, default):
    raw = _env(name, "")
    return default if raw == "" else float(raw)


BASE = (
    _env("PROD6_BASE")
    or "https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones"
).rstrip("/")
URL_BUSCADOR = f"{BASE}/buscador"
URL_DETALLE = f"{BASE}/listar-completo"
# Microservicio público de archivos (no el de contrataciones).
# CATEGORIA_1 = requerimiento / bases, según MAESTRA_TIPO_ARCHIVO del front.
URL_ARCHIVOS = (
    _env("PROD6_ARCHIVOS_BASE")
    or "https://prod6.seace.gob.pe/v1/s8uit-services/archivo/archivos-publico"
).rstrip("/")
COD_CATEGORIA_REQUERIMIENTO = 1

ANIO = _env("PROD6_ANIO") or str(date.today().year)
ESTADO = _env("PROD6_ESTADO") or "2"        # 2 = Vigente
ORDEN = _env("PROD6_ORDEN") or "2"          # 2 = más recientes primero
PAGE_SIZE = _env_int("PROD6_PAGE_SIZE", 100)
MAX_PAGES = _env_int("PROD6_MAX_PAGES", None)
MAX_DETALLES = _env_int("PROD6_MAX_DETALLES", None)
SLEEP_SEC = _env_float("PROD6_SLEEP_SEC", 0.4)
WORKERS = _env_int("PROD6_WORKERS", 6)
TIMEOUT = _env_int("PROD6_TIMEOUT", 60)
REINTENTOS = _env_int("PROD6_REINTENTOS", 3)
# Páginas consecutivas sin nomenclaturas nuevas antes de cortar el delta.
PAGINAS_SIN_NUEVOS = _env_int("PROD6_PAGINAS_SIN_NUEVOS", 3)
HORAS_RADAR = _env_int("EMBUDO_HORAS_RADAR", horas_radar_default())
RESUMEN_FILE = Path(__file__).with_name("prod6_delta_resumen.json")
_PROXY_LOG_HECHO = False


def nueva_sesion():
    global _PROXY_LOG_HECHO
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-ES,es;q=0.9",
        "Referer": "https://prod6.seace.gob.pe/",
    })
    if aplicar_proxy(s) and not _PROXY_LOG_HECHO:
        log_proxy_status("[+] PROD6")
        _PROXY_LOG_HECHO = True
    return s


def get_json(session, url, params=None):
    """GET con reintentos y backoff. Devuelve None si agota los intentos."""
    ultimo = None
    for intento in range(1, REINTENTOS + 1):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 403:
                raise RuntimeError(
                    f"WAF 403 en PROD6 ({url}). Revisa IPRoyal (Country PE) "
                    "o PROXY_USER/PROXY_PASS."
                )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            ultimo = e
            if intento < REINTENTOS:
                time.sleep(SLEEP_SEC * intento * 2)
    print(f"  [!] GET {url} falló tras {REINTENTOS} intentos: {ultimo}")
    return None


# ---------------------------------------------------------------------------
# Paso A — Búsqueda paginada
# ---------------------------------------------------------------------------
def buscar_pagina(session, page, anio=None, page_size=None):
    data = get_json(session, URL_BUSCADOR, params={
        "anio": anio or ANIO,
        "lista_estado_contrato": ESTADO,
        "orden": ORDEN,
        "page": page,
        "page_size": page_size or PAGE_SIZE,
    })
    if not data:
        return [], 0
    filas = data.get("data") or []
    total = (data.get("pageable") or {}).get("totalElements") or 0
    return filas, int(total)


def parse_fec_publica(valor):
    if not valor:
        return None
    s = str(valor).strip()
    for fmt, cut in (("%d/%m/%Y %H:%M:%S", 19), ("%d/%m/%Y %H:%M", 16), ("%d/%m/%Y", 10)):
        try:
            return datetime.strptime(s[:cut], fmt)
        except ValueError:
            continue
    return None


def nomenclatura_de(fila):
    """nroDescripcion es la llave; en el buscador viene como desContratacion."""
    raw = (fila.get("nroDescripcion") or fila.get("desContratacion") or "").strip()
    return raw, normalizar_nomenclatura(raw)


# ---------------------------------------------------------------------------
# Paso C — Detalle
# ---------------------------------------------------------------------------
def obtener_detalle(session, id_contrato):
    """
    GET listar-completo. Devuelve (cab, items, etapas).
    Las etapas viven en uitContratoEtapaProjectionList (no hay que scrapear el DOM).
    """
    data = get_json(session, URL_DETALLE, params={"id_contrato": id_contrato})
    if not data:
        return None, [], []
    if isinstance(data, list):
        data = data[0] if data else {}
    cab = data.get("uitContratoCompletoProjection") or {}
    items = data.get("uitContratoItemProjectionList") or []
    etapas = data.get("uitContratoEtapaProjectionList") or []
    if not cab:
        return None, [], []
    return cab, items, etapas


def listar_archivos_requerimiento(session, id_contrato):
    """
    GET /archivos-publico/listar-archivos-contrato/{id}/{codCategoria=1}
    No descarga el ZIP: solo arma la URL pública de descarga (on-demand).
    """
    url = f"{URL_ARCHIVOS}/listar-archivos-contrato/{id_contrato}/{COD_CATEGORIA_REQUERIMIENTO}"
    data = get_json(session, url)
    if not data:
        return []
    if isinstance(data, dict):
        data = data.get("data") or data.get("archivos") or []
    docs = []
    for row in data or []:
        file_id = row.get("idContratoArchivo")
        if not file_id:
            continue
        nombre = (
            row.get("nombre")
            or row.get("nombreArchivo")
            or row.get("docSolicitado")
            or f"requerimiento_{id_contrato}.zip"
        )
        docs.append({
            "file_id": f"prod6-{file_id}",
            "file_code": str(file_id),
            "categoria": "requerimiento",
            "documento": row.get("descripcion") or "Requerimiento / Bases",
            "nombre_archivo": nombre,
            "url_descarga": f"{URL_ARCHIVOS}/descargar-archivo-contrato/{file_id}",
        })
    return docs


# ---------------------------------------------------------------------------
# Orquestación del módulo
# ---------------------------------------------------------------------------
def ejecutar(anio=None, max_paginas=None, max_detalles=None, full=False,
             conocidas=None, dry_run=False, horas_radar=None):
    """
    Radar 48–72h: solo procesos vigentes recientes. Omite nomenclaturas
    ya en Supabase o marcadas bloqueada por OECE (no gasta proxy en detalle).
    """
    anio = anio or ANIO
    max_paginas = MAX_PAGES if max_paginas is None else max_paginas
    max_detalles = MAX_DETALLES if max_detalles is None else max_detalles
    horas_radar = HORAS_RADAR if horas_radar is None else horas_radar
    corte = datetime.now() - timedelta(hours=horas_radar)
    t0 = time.time()

    print("=" * 75)
    print(f" SEACE PROD6 — RADAR {horas_radar}h | año={anio} estado={ESTADO}")
    print("=" * 75, flush=True)

    bloqueadas = set()
    if conocidas is None:
        try:
            conocidas = nomenclaturas_en_nube()
            bloqueadas = nomenclaturas_bloqueadas()
            conocidas |= bloqueadas
            print(f"[+] Nomenclaturas ya en Supabase: {len(conocidas) - len(bloqueadas)}")
            print(f"[+] Bloqueadas por OECE (se omiten): {len(bloqueadas)}")
        except Exception as e:
            if not dry_run:
                raise SystemExit(
                    f"[-] No se pudo leer Supabase para deduplicar: {e}"
                ) from e
            print(f"[!] Sin dedup ({e}); dry-run continúa sobre todo el listado.")
            conocidas = set()

    session = nueva_sesion()
    vistos_run = set()
    candidatos = []
    page = 1
    total = None
    vistas = 0
    duplicados = 0
    paginas_secas = 0

    # Paso A + B: paginar y deduplicar por nomenclatura normalizada
    while True:
        filas, total_srv = buscar_pagina(session, page, anio=anio)
        if total is None:
            total = total_srv
            print(f"[*] Total en servidor: {total} procesos (page_size={PAGE_SIZE})")
        if not filas:
            print(f"[-] Página {page} vacía — fin del barrido")
            break

        vistas += len(filas)
        nuevos_pagina = 0
        dentro_ventana = 0
        for fila in filas:
            pub = parse_fec_publica(fila.get("fecPublica"))
            if pub is None:
                if not full:
                    continue
            elif pub >= corte:
                dentro_ventana += 1
            elif not full:
                continue
            raw, norm = nomenclatura_de(fila)
            id_contrato = fila.get("idContrato")
            if not norm or not id_contrato:
                continue
            if norm in conocidas or norm in vistos_run or norm in bloqueadas:
                duplicados += 1
                continue
            vistos_run.add(norm)
            candidatos.append({"id_contrato": id_contrato, "nomenclatura": raw,
                               "nomenclatura_norm": norm, "resumen": fila})
            nuevos_pagina += 1

        print(f"[+] pág.{page}: {len(filas)} filas | en_ventana={dentro_ventana} "
              f"| nuevas={nuevos_pagina} | acumulado={len(candidatos)}")

        paginas_secas = paginas_secas + 1 if nuevos_pagina == 0 else 0
        if not full and dentro_ventana == 0 and filas:
            print(f"[*] Página {page} ya está fuera de las {horas_radar}h — corte de radar")
            break
        if not full and paginas_secas >= PAGINAS_SIN_NUEVOS:
            print(f"[*] {paginas_secas} páginas seguidas sin novedades — "
                  "corte de delta (usa --full para barrer todo)")
            break
        if max_detalles is not None and len(candidatos) >= max_detalles:
            candidatos = candidatos[:max_detalles]
            print(f"[*] Tope de detalles alcanzado ({max_detalles})")
            break
        if total and page * PAGE_SIZE >= total:
            break
        if max_paginas is not None and page >= max_paginas:
            print(f"[*] Tope de páginas alcanzado ({max_paginas})")
            break

        page += 1
        time.sleep(SLEEP_SEC)

    try:
        guardar_radar(candidatos, horas_radar, extra={"vistas": vistas, "duplicados": duplicados})
    except OSError as e:
        print(f"[!] No se pudo escribir radar_nuevas.json: {e}")

    if not candidatos:
        print("[=] Sin procesos nuevos en PROD6.")
        return _resumen(anio, vistas, duplicados, 0, 0, 0, t0, horas_radar=horas_radar)

    # Paso C: detalle de cada proceso nuevo (en paralelo, con cortesía)
    print(f"[*] Descargando detalle de {len(candidatos)} procesos nuevos...")

    local = threading.local()

    def _detalle(c):
        if not getattr(local, "session", None):
            local.session = nueva_sesion()
        cab, items, etapas = obtener_detalle(local.session, c["id_contrato"])
        docs = listar_archivos_requerimiento(local.session, c["id_contrato"]) if cab else []
        time.sleep(SLEEP_SEC)
        if not cab:
            return None
        return {
            "cab": cab,
            "resumen": c["resumen"],
            "items": items,
            "etapas": etapas,
            "docs": docs,
        }

    procesos = []
    fallidos = 0
    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as pool:
        for i, res in enumerate(pool.map(_detalle, candidatos), start=1):
            if res is None:
                fallidos += 1
                continue
            procesos.append(res)
            if i % 25 == 0 or i == len(candidatos):
                print(f"    detalle {i}/{len(candidatos)} (fallidos={fallidos})")

    # Paso D: upsert por lotes (convocatorias primero, luego hijas).
    # cronograma_proceso no existe en este Supabase: no se mapea ni se inserta.
    from supabase_sync import convocatoria_from_prod6, items_from_prod6

    procesos_validos = []
    noms_lote = set()
    for p in procesos:
        conv = convocatoria_from_prod6(
            p["cab"], p.get("resumen"), etapas=p.get("etapas"), docs=p.get("docs")
        )
        if not conv or not conv.get("nomenclatura_norm"):
            continue
        nom = conv["nomenclatura_norm"]
        if nom in noms_lote:
            continue
        noms_lote.add(nom)
        p = dict(p)
        p["nomenclatura_norm"] = nom
        procesos_validos.append(p)
    omitidos_sin_nom = len(procesos) - len(procesos_validos)
    if omitidos_sin_nom:
        print(
            f"[!] PROD6: {omitidos_sin_nom} procesos sin nomenclatura_norm "
            "válida — no se upsertan hijas huérfanas"
        )
    procesos = procesos_validos

    insertados = 0
    items_total = 0
    if dry_run:
        print("[*] DRY-RUN: no se escribe en Supabase. Muestra del mapeo:")
        for p in procesos[:3]:
            conv = convocatoria_from_prod6(
                p["cab"], p["resumen"], etapas=p.get("etapas"), docs=p.get("docs")
            )
            n_items = len(items_from_prod6(conv["nomenclatura_norm"], p["items"]))
            # cronograma_proceso no existe aún — se omite el mapeo/upsert.
            # crono = cronograma_from_prod6(conv["nomenclatura_norm"], p.get("etapas"))
            print(json.dumps(conv, ensure_ascii=False, indent=2))
            print(f"    items={n_items} docs={len(p.get('docs') or [])}")
            # print(json.dumps(crono, ensure_ascii=False, indent=2))
        items_total = sum(len(p.get("items") or []) for p in procesos)
        return _resumen(anio, vistas, duplicados, len(candidatos), 0, items_total,
                        t0, fallidos=fallidos, horas_radar=horas_radar)

    for i in range(0, len(procesos), LOTE_UPSERT):
        lote = procesos[i : i + LOTE_UPSERT]
        try:
            ok, n_items = upsert_prod6_lote(lote)
        except Exception as e:
            print(f"[!] Upsert lote PROD6 ({len(lote)}): {e}")
            continue
        insertados += ok
        items_total += n_items

    resumen = _resumen(anio, vistas, duplicados, len(candidatos), insertados,
                       items_total, t0, fallidos=fallidos, horas_radar=horas_radar)
    print("---")
    print(f"[+] PROD6 | vistas={vistas} duplicados={duplicados} "
          f"nuevas={len(candidatos)} inyectadas={insertados} items={items_total}")
    return resumen


def _resumen(anio, vistas, duplicados, nuevas, insertados, items, t0, fallidos=0,
             horas_radar=None):
    data = {
        "fuente": "PROD6",
        "anio": str(anio),
        "horas_radar": horas_radar if horas_radar is not None else HORAS_RADAR,
        "vistas": vistas,
        "duplicados": duplicados,
        "nuevas": nuevas,
        "insertados": insertados,
        "items": items,
        "detalles_fallidos": fallidos,
        "segundos": round(time.time() - t0, 2),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        RESUMEN_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        print(f"[!] No se pudo escribir {RESUMEN_FILE.name}: {e}")
    return data


def parse_args():
    p = argparse.ArgumentParser(
        description="SEACE PROD6 — Compras Menores (<= 8 UIT) vía API REST"
    )
    p.add_argument("--anio", default=ANIO)
    p.add_argument("--max-paginas", type=int, default=MAX_PAGES)
    p.add_argument("--max-detalles", type=int, default=MAX_DETALLES)
    p.add_argument(
        "--full",
        action="store_true",
        help="Barre todas las páginas aunque no aparezcan nomenclaturas nuevas",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Extrae y mapea, pero no escribe en Supabase",
    )
    p.add_argument(
        "--horas-radar",
        type=int,
        default=HORAS_RADAR,
        help="Solo procesos publicados en las últimas N horas (default 72)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    resultado = ejecutar(
        anio=args.anio,
        max_paginas=args.max_paginas,
        max_detalles=args.max_detalles,
        full=args.full,
        dry_run=args.dry_run,
        horas_radar=args.horas_radar,
    )
    sys.exit(0 if resultado is not None else 1)
