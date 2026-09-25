"""Embudo de ahorro: no reconsultar en SEACE lo que OECE ya cerró."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

RADAR_FILE = Path(__file__).with_name("radar_nuevas.json")

# Palabras de estado terminal (OECE OCDS + textos SEACE).
_TERMINAL = re.compile(
    r"(finaliz|culminad|otorgad|adjudicad|buena\s*pro|cancelad|anulad|"
    r"desiert|desert|unsuccessful|withdrawn|complete|cancelled)",
    re.IGNORECASE,
)

_OCDS_A_ESTADO = {
    "complete": "Finalizada",
    "completed": "Finalizada",
    "cancelled": "Cancelada",
    "canceled": "Cancelada",
    "withdrawn": "Cancelada",
    "unsuccessful": "Desierta",
    "active": "Vigente",
    "planned": "Vigente",
    "planning": "Vigente",
}


def clasificar_estado_oece(status_raw, tiene_ganador=False):
    """Devuelve (estado_es, bloqueada). Un ganador implica Otorgada/bloqueada."""
    if tiene_ganador:
        return "Otorgada", True
    key = (status_raw or "").strip().lower()
    estado = _OCDS_A_ESTADO.get(key, (status_raw or "").strip() or "Vigente")
    return estado, es_estado_terminal(estado) or es_estado_terminal(key)


def es_estado_terminal(valor) -> bool:
    if valor is True:
        return True
    if valor is False or valor is None:
        return False
    return bool(_TERMINAL.search(str(valor)))


def estado_nunca_nulo(valor, *fechas_limite):
    """estado nunca sale vacío/None/NaN. Si no hay texto, infiere Vigente/Cerrado."""
    if valor is not None and valor is not False:
        s = str(valor).strip()
        if s and s.lower() not in ("nan", "none", "null", "nat"):
            return s
    limite = None
    for raw in fechas_limite:
        dt = _parse_dt_suelto(raw)
        if dt and (limite is None or dt > limite):
            limite = dt
    if limite and limite < datetime.now(limite.tzinfo):
        return "Cerrado"
    return "Vigente"


def _parse_dt_suelto(valor):
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor
    s = str(valor).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    for fmt, cut in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%d/%m/%Y %H:%M", 16),
        ("%Y-%m-%d", 10),
        ("%d/%m/%Y", 10),
    ):
        try:
            return datetime.strptime(s[:cut], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def clasificar_nombre_etapa(nombre):
    n = (nombre or "").lower()
    if "integrac" in n or "consolidac" in n:
        return "integracion"
    if "present" in n and any(x in n for x in ("ofert", "propuest", "cotiz")):
        return "presentacion"
    if "present" in n and "consulta" not in n:
        return "presentacion"
    if "cotiz" in n:
        return "cotizacion"
    if "consulta" in n or "observac" in n:
        return "consultas"
    return None


def horas_radar_default() -> int:
    raw = (os.environ.get("EMBUDO_HORAS_RADAR") or "72").strip()
    try:
        return max(24, int(raw))
    except ValueError:
        return 72


def guardar_radar(nuevas, horas, extra=None):
    payload = {
        "fuente": "PROD6",
        "horas_radar": horas,
        "nomenclaturas_norm": [
            n.get("nomenclatura_norm") for n in nuevas if n.get("nomenclatura_norm")
        ],
        "nuevas": nuevas,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    RADAR_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[+] Radar PROD6: {len(payload['nomenclaturas_norm'])} nuevas → {RADAR_FILE.name}")
    return payload


def leer_radar_nomenclaturas(path=None) -> set:
    p = Path(path) if path else RADAR_FILE
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    items = data.get("nomenclaturas_norm") or []
    return {str(x) for x in items if x}
