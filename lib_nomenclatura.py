"""Llave primaria compartida OECE / SEACE: nomenclatura normalizada."""
from __future__ import annotations

import re

_NOM_RE = re.compile(
    r"([A-ZÁÉÍÓÚÑ]{1,20}[-/][A-ZÁÉÍÓÚÑ0-9./]{1,30}(?:[-/][A-ZÁÉÍÓÚÑ0-9./]{1,30}){2,10})",
    re.IGNORECASE,
)


def normalizar_nomenclatura(valor):
    if valor is None:
        return ""
    return re.sub(r"\s+", "", str(valor).upper().strip())


def extraer_nomenclatura(*candidatos):
    for raw in candidatos:
        if not raw:
            continue
        text = str(raw).strip()
        if not text or text.lower() in ("none", "null"):
            continue
        m = _NOM_RE.search(text)
        picked = m.group(1) if m else text
        norm = normalizar_nomenclatura(picked)
        if len(norm) >= 8:
            return picked.strip(), norm
    return "", ""
