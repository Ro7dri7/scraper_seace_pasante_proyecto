"""
Pipeline unificado OECE + SEACE (todo en este mismo proyecto).

Pasos:
  1) Extracción masiva OECE (API CSV)
  2) Contactos RNP
  3) Centralización
  4) Ubicación SUNAT
  5) Delta SEACE (2Captcha + dedup nomenclaturas + Supabase)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run_step(step_num, title, cmd, cwd=None):
    cwd = Path(cwd or ROOT)
    print("\n" + "=" * 75)
    print(f" PASO {step_num}: {title}")
    print(f" Comando: {' '.join(str(c) for c in cmd)}")
    print(f" CWD: {cwd}")
    print("=" * 75, flush=True)
    t0 = time.time()

    ret = subprocess.run(cmd, cwd=str(cwd))
    if ret.returncode != 0:
        print(
            f"\n[!] Error en el Paso {step_num} "
            f"(código de salida: {ret.returncode}). Se detiene el pipeline."
        )
        sys.exit(ret.returncode)

    print(
        f"[OK] Paso {step_num} completado exitosamente "
        f"en {time.time() - t0:.2f} segundos.\n",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Pipeline unificado OECE + SEACE (proyecto único)"
    )
    parser.add_argument("--year", type=str, default="2026")
    parser.add_argument("--month", type=str, default="09")
    parser.add_argument("--min-days", type=int, default=0)
    parser.add_argument("--workers-rnp", type=int, default=35)
    parser.add_argument("--workers-sunat", type=int, default=60)
    parser.add_argument(
        "--skip-seace",
        action="store_true",
        help="Solo OECE (sin delta SEACE)",
    )
    parser.add_argument(
        "--max-fichas-seace",
        type=int,
        default=None,
        help="Tope de fichas SEACE (prueba). Sin flag = sin tope.",
    )
    args = parser.parse_args()

    year = str(args.year).strip()
    month = str(args.month).zfill(2)
    periodo = f"{year}_{month}"
    py = sys.executable

    print("*" * 75)
    print(f" INICIANDO PIPELINE MAESTRO - PERIODO {year}-{month}")
    print(f" ROOT: {ROOT}")
    print("*" * 75, flush=True)
    t_total = time.time()

    base_prefix = f"prospectos_seace_{periodo}"
    raw_csv = f"{base_prefix}.csv"
    raw_xlsx = f"{base_prefix}.xlsx"
    dir_csv = f"{base_prefix}_DIRECTORIO_RUC.csv"
    dir_xlsx = f"{base_prefix}_DIRECTORIO_RUC.xlsx"
    cent_csv = f"{base_prefix}_CENTRALIZADO.csv"
    cent_xlsx = f"{base_prefix}_CENTRALIZADO.xlsx"

    run_step(
        1,
        f"Extracción Masiva OECE ({year}-{month})",
        [
            py,
            "extractor_seace_oece.py",
            "--year",
            year,
            "--month",
            month,
            "--min-days",
            str(args.min_days),
        ],
    )

    run_step(
        2,
        "Enriquecimiento de Teléfonos y Correos Oficiales (RNP / OECE)",
        [
            py,
            "enriquecer_contactos_rnp.py",
            "--input",
            raw_csv,
            "--solo-unicos",
            "--workers",
            str(args.workers_rnp),
        ],
    )

    run_step(
        3,
        "Centralización Maestra de Licitaciones + Contactos",
        [py, "centralizar_data_seace.py", "--input", raw_xlsx],
    )

    run_step(
        4,
        "Enriquecimiento de Ubicación Fiscal y Estado Tributario (SUNAT)",
        [
            py,
            "enriquecer_ubicacion_sunat.py",
            "--directorio",
            dir_csv,
            "--centralizado",
            cent_csv,
            "--workers",
            str(args.workers_sunat),
        ],
    )

    if args.skip_seace:
        print("[*] Delta SEACE omitido (--skip-seace).")
    else:
        state_path = ROOT / "handoff_state.json"
        monthly_handoff = ROOT / f"{base_prefix}_handoff.json"
        handoff_path = state_path if state_path.exists() else monthly_handoff
        if not handoff_path.exists():
            print(
                f"[!] No hay handoff_state.json ni {monthly_handoff.name}; "
                "no se lanza SEACE."
            )
        else:
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
            fecha_max = (
                handoff.get("fecha_max_seace")
                or handoff.get("fecha_max_iso")
                or handoff.get("fecha_max")
            )
            if not fecha_max:
                print("[!] handoff sin fecha_max; no se lanza SEACE.")
            else:
                cmd = [
                    py,
                    "main.py",
                    "--desde",
                    str(fecha_max),
                    "--year",
                    year,
                    "--objeto",
                    "",
                    "--nomenclaturas-file",
                    str(handoff_path),
                ]
                if args.max_fichas_seace is not None:
                    cmd.extend(["--max-fichas", str(args.max_fichas_seace)])
                run_step(
                    5,
                    f"Delta SEACE desde {fecha_max} hasta hoy (dedup + Supabase)",
                    cmd,
                )

    print("*" * 75)
    print(f" ¡PIPELINE DE {year}-{month} COMPLETADO CON ÉXITO!")
    print(f" Tiempo total: {time.time() - t_total:.2f} segundos.")
    print(" Archivos finales:")
    print(f"   1. {cent_xlsx}")
    print(f"   2. {dir_xlsx}")
    print("   3. handoff_state.json")
    print("   4. Supabase: convocatorias / proveedores / documentos_proceso")
    print("*" * 75, flush=True)


if __name__ == "__main__":
    main()
