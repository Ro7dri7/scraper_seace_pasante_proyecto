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
