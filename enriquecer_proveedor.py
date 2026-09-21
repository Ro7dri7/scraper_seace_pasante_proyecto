"""
Enriquecimiento comercial de un RUC ANTES del upsert a proveedores.

Fuentes oficiales OECE (las mismas que ya usa el Excel):
  - RNP:   GET https://eap.oece.gob.pe/perfilprov-bus/1.0/ficha/{RUC}
  - SUNAT: GET https://eap.oece.gob.pe/ficha-proveedor-cns/1.0/ficha/{RUC}/resumen

Por qué no API Perú ni scraping SUNAT:
  - Ya tenemos estas APIs públicas, con caché y workers.
  - API Perú es de pago y otro SLA.
  - El padrón web de SUNAT bloquea y no escala para miles de RUCs.
  - Un CSV estático se pudre (bajas, NO HABIDO). El CRM necesita estado vivo.

Si el HTTP falla, se usa caché de disco; si tampoco hay, se inserta el RUC
con estado_sunat='N/D' (nunca se omite el lead).
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from enriquecer_contactos_rnp import (
    fetch_proveedor_ficha,
    load_cache as load_rnp_cache,
    save_cache as save_rnp_cache,
)
from enriquecer_ubicacion_sunat import (
    fetch_sunat_location,
    load_cache as load_sunat_cache,
    save_cache as save_sunat_cache,
)

WORKERS = int(os.environ.get("ENRIQUECER_WORKERS") or "20")


def _limpio(valor, default=""):
    if valor is None:
        return default
    s = str(valor).strip()
    if s.lower() in ("", "null", "none", "n/d", "no registra", "undefined"):
        return default
    return s


def ficha_comercial(ruc: str, rnp_cache=None, sunat_cache=None) -> dict:
    """Una ficha lista para columnas de public.proveedores."""
    ruc = str(ruc or "").replace("PE-RUC-", "").strip()
    vacio = {
        "telefono": "",
        "email": "",
        "telefono_rnp": "",
        "email_rnp": "",
        "departamento": "",
        "provincia": "",
        "distrito": "",
        "estado_sunat": "N/D",
        "condicion_domicilio": "N/D",
        "habilitado_rnp": "No",
        "apto_contratar": "No",
        "ficha_rnp_url": f"https://apps.oece.gob.pe/perfilprov-ui/ficha/{ruc}" if ruc else "",
    }
    if len(ruc) != 11 or not ruc.isdigit():
        return vacio

    if rnp_cache is not None and ruc in rnp_cache:
        rnp = rnp_cache[ruc]
    else:
        _, rnp = fetch_proveedor_ficha(ruc)
        if rnp_cache is not None:
            rnp_cache[ruc] = rnp

    if sunat_cache is not None and ruc in sunat_cache:
        sunat = sunat_cache[ruc]
    else:
        _, sunat = fetch_sunat_location(ruc)
        if sunat_cache is not None:
            sunat_cache[ruc] = sunat

    tel = _limpio(rnp.get("telefono"))
    mail = _limpio(rnp.get("email"))
    return {
        "telefono": tel,
        "email": mail,
        "telefono_rnp": tel,
        "email_rnp": mail,
        "departamento": _limpio(sunat.get("departamento")),
        "provincia": _limpio(sunat.get("provincia")),
        "distrito": _limpio(sunat.get("distrito")),
        "estado_sunat": _limpio(sunat.get("estado_sunat"), "N/D").upper(),
        "condicion_domicilio": _limpio(sunat.get("condicion_sunat"), "N/D").upper(),
        "habilitado_rnp": _limpio(rnp.get("habilitado"), "No"),
        "apto_contratar": _limpio(rnp.get("apto"), "No"),
        "ficha_rnp_url": rnp.get("ficha_url") or vacio["ficha_rnp_url"],
    }


def enriquecer_rucs(rucs, workers=None) -> dict[str, dict]:
    """
    Consulta RUCs únicos en paralelo. Devuelve {ruc: ficha_comercial}.
    Reutiliza cache_contactos_rnp.json y cache_ubicaciones_sunat.json.
    """
    unicos = []
    vistos = set()
    for raw in rucs:
        r = str(raw or "").replace("PE-RUC-", "").strip()
        if r and r not in vistos:
            vistos.add(r)
            unicos.append(r)

    rnp_cache = load_rnp_cache()
    sunat_cache = load_sunat_cache()
    faltan = [r for r in unicos if r not in rnp_cache or r not in sunat_cache]
    print(
        f"[*] Enriquecimiento RNP/SUNAT: {len(unicos)} RUCs "
        f"({len(unicos) - len(faltan)} en caché, {len(faltan)} a consultar)"
    )

    def _uno(ruc):
        return ruc, ficha_comercial(ruc, rnp_cache, sunat_cache)

    out = {}
    n_workers = max(1, workers or WORKERS)
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for i, (ruc, ficha) in enumerate(pool.map(_uno, unicos), start=1):
            out[ruc] = ficha
            if i % 50 == 0 or i == len(unicos):
                print(f"    ficha comercial {i}/{len(unicos)}")

    save_rnp_cache(rnp_cache)
    save_sunat_cache(sunat_cache)
    return out
