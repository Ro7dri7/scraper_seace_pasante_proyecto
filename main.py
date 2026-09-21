"""
LicitApp — flujo maestro de las 3 fuentes (embudo de ahorro).

Orden:
  1) OECE            — bulk OCDS, SIN proxy. Marca bloqueada si está cerrada.
  2) SEACE PROD6     — radar 48–72h, proxy IPRoyal, sin 2Captcha.
  3) SEACE PROD2     — radar 48–72h, IPRoyal + 2Captcha; ficha solo de nuevas no bloqueadas.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def contar_convocatorias():
    """Total de filas en Supabase. Devuelve None si la nube no responde."""
    try:
        from supabase_sync import contar_convocatorias as _contar

        return _contar()
    except Exception as e:
        print(f"[!] No se pudo contar convocatorias en Supabase: {e}")
        return None


def correr(titulo, cmd, obligatorio=False):
    """Ejecuta una etapa como subproceso. Devuelve el código de salida."""
    print("\n" + "=" * 75)
    print(f" {titulo}")
    print(f" Comando: {' '.join(str(c) for c in cmd)}")
    print("=" * 75, flush=True)
    t0 = time.time()
    ret = subprocess.run(cmd, cwd=str(ROOT))
    dur = time.time() - t0
    if ret.returncode != 0:
        print(f"[!] {titulo} terminó con código {ret.returncode} ({dur:.1f}s)")
        if obligatorio:
            sys.exit(ret.returncode)
    else:
        print(f"[OK] {titulo} ({dur:.1f}s)", flush=True)
    return ret.returncode


def leer_resumen(nombre):
    path = ROOT / nombre
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def fecha_max_handoff(base_prefix):
    """Posta OECE→PROD2: fecha desde la que arranca el delta de prod2."""
    state = ROOT / "handoff_state.json"
    mensual = ROOT / f"{base_prefix}_handoff.json"
    path = state if state.exists() else mensual
    if not path.exists():
        return None, None
    try:
        handoff = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Handoff ilegible ({path.name}): {e}")
        return None, None
    fecha = (
        handoff.get("fecha_max_seace")
        or handoff.get("fecha_max_iso")
        or handoff.get("fecha_max")
    )
    return fecha, path


# ---------------------------------------------------------------------------
# Etapas
# ---------------------------------------------------------------------------
def etapa_oece(py, args):
    """Bulk histórico OECE. Con enriquecimiento reusa el pipeline completo."""
    if args.sin_enriquecimiento:
        cmd = [
            py, "extractor_seace_oece.py",
            "--year", args.year,
            "--month", args.month,
            "--min-days", str(args.min_days),
        ]
    else:
        cmd = [
            py, "pipeline_seace.py",
            "--year", args.year,
            "--month", args.month,
            "--min-days", str(args.min_days),
            "--workers-rnp", str(args.workers_rnp),
            "--workers-sunat", str(args.workers_sunat),
            "--skip-seace",
        ]
    correr(f"FUENTE 1/3 · OECE (bulk, sin proxy) {args.year}-{args.month}", cmd, obligatorio=True)


def etapa_prod2(py, args, base_prefix):
    """Francotirador: radar 48–72h, ficha 2Captcha solo de nomenclaturas nuevas no bloqueadas."""
    _, handoff_path = fecha_max_handoff(base_prefix)
    cmd = [
        py, "scraper_prod2.py",
        "--year", args.year,
        "--objeto", "",
        "--horas-radar", str(args.horas_radar),
    ]
    if handoff_path:
        cmd += ["--nomenclaturas-file", str(handoff_path)]
    if args.max_fichas_prod2 is not None:
        cmd += ["--max-fichas", str(args.max_fichas_prod2)]
    correr("FUENTE 3/3 · SEACE PROD2 (radar + 2Captcha)", cmd)


def etapa_prod6(py, args):
    """Radar: últimas 48–72h, proxy IPRoyal, sin CAPTCHA. Omite bloqueadas."""
    cmd = [
        py, "scraper_prod6.py",
        "--anio", args.year,
        "--horas-radar", str(args.horas_radar),
    ]
    if args.max_detalles_prod6 is not None:
        cmd += ["--max-detalles", str(args.max_detalles_prod6)]
    if args.prod6_full:
        cmd += ["--full"]
    correr("FUENTE 2/3 · SEACE PROD6 (radar 48–72h, proxy)", cmd)


# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description="LicitApp — orquestador OECE + SEACE PROD2 + SEACE PROD6"
    )
    p.add_argument("--year", default=str(time.localtime().tm_year))
    p.add_argument("--month", default=f"{time.localtime().tm_mon:02d}")
    p.add_argument("--min-days", type=int, default=0)
    p.add_argument("--workers-rnp", type=int, default=35)
    p.add_argument("--workers-sunat", type=int, default=60)
    p.add_argument("--skip-oece", action="store_true")
    p.add_argument("--skip-prod2", action="store_true")
    p.add_argument("--skip-prod6", action="store_true")
    p.add_argument(
        "--sin-enriquecimiento",
        action="store_true",
        help="OECE solo extractor (omite RNP / centralización / SUNAT)",
    )
    p.add_argument("--max-fichas-prod2", type=int, default=None)
    p.add_argument("--max-detalles-prod6", type=int, default=None)
    p.add_argument(
        "--prod6-full",
        action="store_true",
        help="PROD6 barre todas las páginas aunque no haya nomenclaturas nuevas",
    )
    p.add_argument(
        "--horas-radar",
        type=int,
        default=72,
        help="Ventana de publicación PROD6/PROD2 en horas (48–72)",
    )
    args = p.parse_args()
    args.year = str(args.year).strip()
    args.month = f"{int(args.month):02d}"
    return args


def main():
    args = parse_args()
    py = sys.executable
    base_prefix = f"prospectos_seace_{args.year}_{args.month}"
    t_total = time.time()

    print("*" * 75)
    print(f" LICITAPP — MOTOR UNIFICADO | PERIODO {args.year}-{args.month}")
    print(" Embudo: OECE (gratis) → PROD6 (proxy) → PROD2 (proxy+2Captcha).")
    print("*" * 75, flush=True)

    base = contar_convocatorias()
    if base is None:
        print("[!] Sin conteo inicial: los totales por fuente saldrán como 'n/d'.")
    print(f"[*] Convocatorias en Supabase al inicio: {base if base is not None else 'n/d'}")

    etapas = [
        ("OECE (bulk histórico)", args.skip_oece, lambda: etapa_oece(py, args)),
        ("SEACE PROD6 (radar menor)", args.skip_prod6, lambda: etapa_prod6(py, args)),
        ("SEACE PROD2 (radar mayor)", args.skip_prod2,
         lambda: etapa_prod2(py, args, base_prefix)),
    ]

    previo = base
    inyectados = {}
    for nombre, saltar, ejecutar in etapas:
        if saltar:
            print(f"\n[*] {nombre}: omitida por flag.")
            inyectados[nombre] = "omitida"
            continue
        ejecutar()
        actual = contar_convocatorias()
        if previo is None or actual is None:
            inyectados[nombre] = "n/d"
        else:
            inyectados[nombre] = max(0, actual - previo)
            print(f"[+] {nombre}: +{inyectados[nombre]} procesos nuevos en Supabase")
        previo = actual if actual is not None else previo

    resumen_prod2 = leer_resumen("seace_delta_resumen.json")
    resumen_prod6 = leer_resumen("prod6_delta_resumen.json")

    print("\n" + "*" * 75)
    print(" RESUMEN DE INYECCIÓN POR FUENTE")
    print("*" * 75)
    for nombre, _saltar, _fn in etapas:
        n = inyectados.get(nombre)
        detalle = n if isinstance(n, str) else f"+{n} procesos nuevos"
        print(f"  {nombre:<30} : {detalle}")
    if resumen_prod2:
        print(f"  · PROD2 vistas={resumen_prod2.get('vistas')} "
              f"nuevas={resumen_prod2.get('nuevas')} "
              f"dup_oece={resumen_prod2.get('duplicados_oece')}")
    if resumen_prod6:
        print(f"  · PROD6 vistas={resumen_prod6.get('vistas')} "
              f"nuevas={resumen_prod6.get('nuevas')} "
              f"items={resumen_prod6.get('items')}")
    print(f"  Convocatorias totales en Supabase : "
          f"{previo if previo is not None else 'n/d'}"
          f" (inicio: {base if base is not None else 'n/d'})")
    print(f"  Tiempo total                      : {time.time() - t_total:.1f}s")
    print("*" * 75, flush=True)


if __name__ == "__main__":
    main()
