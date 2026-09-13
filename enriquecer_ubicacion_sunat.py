"""
Enriquecedor de Ubicación Fiscal SUNAT (Departamento, Provincia, Distrito)
========================================================================
Consulta el microservicio oficial de Ficha Única del Proveedor (OECE / SUNAT):
GET https://eap.oece.gob.pe/ficha-proveedor-cns/1.0/ficha/{RUC}/resumen

Extrae:
- Departamento
- Provincia
- Distrito
- Estado SUNAT (ACTIVO / BAJA)
- Condición Domicilio (HABIDO / NO HABIDO)

Características:
- Filtrado estricto por RUC único (cero consultas duplicadas).
- Caché persistente en disco ('cache_ubicaciones_sunat.json').
- Multihilo de alta velocidad (40 workers).
- Actualiza automáticamente tanto el DIRECTORIO_RUC como el archivo CENTRALIZADO.
"""

import os
import ssl
import csv
import json
import time
import argparse
import urllib.request
import concurrent.futures

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

CACHE_FILE = "cache_ubicaciones_sunat.json"

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Origin': 'https://apps.oece.gob.pe'
}


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(cache):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def clean_val(v, default="No registra"):
    if v is None:
        return default
    s = str(v).strip()
    if s.lower() in ["", "null", "none", "n/d", "undefined"]:
        return default
    return s.upper()


def fetch_sunat_location(ruc, retries=3):
    """Consulta los datos de ubicación fiscal de SUNAT vía OECE con reintentos automáticos."""
    clean_r = str(ruc).replace("PE-RUC-", "").strip()
    if len(clean_r) != 11 or not clean_r.isdigit() or clean_r.startswith("99"):
        return clean_r, {
            "departamento": "No registra",
            "provincia": "No registra",
            "distrito": "No registra",
            "estado_sunat": "N/D",
            "condicion_sunat": "N/D"
        }

    url = f"https://eap.oece.gob.pe/ficha-proveedor-cns/1.0/ficha/{clean_r}/resumen"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=8) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                ds = data.get("datosSunat") or {}
                dep = clean_val(ds.get("departamento"))
                prov = clean_val(ds.get("provincia"))
                dist = clean_val(ds.get("distrito"))
                estado = clean_val(ds.get("estado"), "N/D")
                cond = clean_val(ds.get("condicion"), "N/D")

                return clean_r, {
                    "departamento": dep,
                    "provincia": prov,
                    "distrito": dist,
                    "estado_sunat": estado,
                    "condicion_sunat": cond
                }
        except Exception:
            if attempt < retries - 1:
                time.sleep(0.4 * (attempt + 1))

    return clean_r, {
        "departamento": "No registra",
        "provincia": "No registra",
        "distrito": "No registra",
        "estado_sunat": "N/D",
        "condicion_sunat": "N/D"
    }


def enrich_locations(directory_file="prospectos_seace_2026_07_3887_DIRECTORIO_RUC.csv",
                     centralized_file="prospectos_seace_2026_07_3887_CENTRALIZADO.csv",
                     limit=None,
                     workers=60):
    t0 = time.time()
    print("=" * 75, flush=True)
    print(" ENRIQUECEDOR DE UBICACIÓN FISCAL (SUNAT / OECE)", flush=True)
    print(" [Departamento, Provincia, Distrito, Estado SUNAT, Condición Domicilio]", flush=True)
    print("=" * 75, flush=True)
    print(f" Archivo Directorio    : {directory_file}", flush=True)
    print(f" Archivo Centralizado  : {centralized_file}", flush=True)
    print(f" Hilos paralelos       : {workers}", flush=True)
    print("=" * 75 + "\n", flush=True)

    # 1. Extraer RUCs únicos del directorio
    unique_rucs = []
    directory_rows = []
    if os.path.exists(directory_file):
        with open(directory_file, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                directory_rows.append(r)
                ruc = r.get("RUC", "").strip()
                if ruc and len(ruc) == 11 and ruc.isdigit() and ruc not in unique_rucs:
                    unique_rucs.append(ruc)
        print(f"[+] Directorio cargado: {len(directory_rows):,} filas ({len(unique_rucs):,} RUCs únicos)", flush=True)
    else:
        print(f"[!] Archivo de directorio '{directory_file}' no encontrado.", flush=True)
        return

    if limit and limit > 0:
        unique_rucs = unique_rucs[:limit]
        print(f"[*] Modo de prueba: Procesando las primeras {len(unique_rucs)} empresas...", flush=True)

    # 2. Consultar Caché
    cache = load_cache()
    pending = [r for r in unique_rucs if r not in cache]
    cached_count = len(unique_rucs) - len(pending)

    print(f"\n[*] Estado de Caché Local:", flush=True)
    print(f"    -> Ya disponibles en disco (0s espera): {cached_count:,} empresas", flush=True)
    print(f"    -> Pendientes a consultar en API SUNAT: {len(pending):,} empresas", flush=True)

    # 3. Consulta concurrente a la API de SUNAT/OECE
    if pending:
        t_net = time.time()
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(fetch_sunat_location, r): r for r in pending}
            for f in concurrent.futures.as_completed(future_map):
                done += 1
                try:
                    ruc, info = f.result()
                    cache[ruc] = info
                    if done % 100 == 0 or done == len(pending):
                        pct = (done / len(pending)) * 100
                        print(f"    [*] Progreso SUNAT: {done:,}/{len(pending):,} ({pct:.1f}%)", end="\r", flush=True)
                    if done % 250 == 0:
                        save_cache(cache)
                except Exception:
                    pass
        save_cache(cache)
        print(flush=True)
        print(f"[+] Consultas finalizadas en {time.time() - t_net:.2f} segundos.", flush=True)

    # Resumen de ubicaciones encontradas
    with_dep = sum(1 for r in unique_rucs if cache.get(r, {}).get("departamento") != "No registra")
    print(f"[+] Empresas con Ubicación Identificada: {with_dep:,}/{len(unique_rucs):,} ({(with_dep/len(unique_rucs))*100:.1f}%)")

    # 4. Actualizar el archivo DIRECTORIO_RUC
    print(f"\n[*] Actualizando archivo de Directorio: {directory_file}...")
    updated_dir_rows = []
    dir_headers = [
        "RUC", "Razón Social", "Teléfono / Celular (RNP)", "Email de Contacto (RNP)",
        "Departamento", "Provincia", "Distrito", "Estado SUNAT", "Condición Domicilio",
        "Habilitado RNP", "Apto para Contratar", "Total Postulaciones", "Licitaciones Ganadas",
        "Tasa de Éxito", "Monto Total Concursado (S/.)", "Ejemplos de Licitaciones", "Ficha RNP (Web)"
    ]

    for row in directory_rows:
        r = row.get("RUC", "").strip()
        loc = cache.get(r, {
            "departamento": "No registra",
            "provincia": "No registra",
            "distrito": "No registra",
            "estado_sunat": "N/D",
            "condicion_sunat": "N/D"
        })
        row["Departamento"] = loc["departamento"]
        row["Provincia"] = loc["provincia"]
        row["Distrito"] = loc["distrito"]
        row["Estado SUNAT"] = loc["estado_sunat"]
        row["Condición Domicilio"] = loc["condicion_sunat"]
        updated_dir_rows.append(row)

    # Guardar CSV Directorio
    with open(directory_file, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=dir_headers)
        writer.writeheader()
        writer.writerows(updated_dir_rows)
    print(f"[+] Directorio CSV actualizado: {directory_file}")

    # Guardar Excel Directorio
    dir_xlsx = directory_file.replace(".csv", ".xlsx")
    export_excel(updated_dir_rows, dir_headers, dir_xlsx, sheet_title="Directorio con Ubicación")

    # 5. Actualizar el archivo CENTRALIZADO (50,553 filas)
    if os.path.exists(centralized_file):
        print(f"\n[*] Actualizando archivo Centralizado Maestro: {centralized_file}...")
        cent_headers = [
            "RUC", "Razón Social", "Condición",
            "Teléfono / Celular (RNP)", "Email de Contacto (RNP)",
            "Departamento", "Provincia", "Distrito", "Estado SUNAT", "Condición Domicilio",
            "Habilitado RNP", "Apto para Contratar", "Ficha RNP (Web)",
            "Licitación", "Entidad Convocante", "Objeto de Contratación", "Objeto / Descripción", "Tipo Procedimiento",
            "Categoría", "Monto Referencial (S/.)", "Fecha Convocatoria", "Días Transcurridos",
            "Tipo Documento", "Ver Proceso (Portal OECE)", "URL Bases (PDF Original)", "OCID", "Requiere ISO"
        ]

        cent_rows = []
        with open(centralized_file, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                r = row.get("RUC", "").strip()
                loc = cache.get(r, {
                    "departamento": "No registra",
                    "provincia": "No registra",
                    "distrito": "No registra",
                    "estado_sunat": "N/D",
                    "condicion_sunat": "N/D"
                })
                row["Departamento"] = loc["departamento"]
                row["Provincia"] = loc["provincia"]
                row["Distrito"] = loc["distrito"]
                row["Estado SUNAT"] = loc["estado_sunat"]
                row["Condición Domicilio"] = loc["condicion_sunat"]
                cent_rows.append(row)

        # Ordenar estrictamente por Fecha Convocatoria descendente (las más recientes primero)
        cent_rows.sort(key=lambda x: (str(x.get("Fecha Convocatoria") or ""), str(x.get("Licitación") or "")), reverse=True)

        # Guardar CSV Centralizado
        with open(centralized_file, mode="w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cent_headers)
            writer.writeheader()
            writer.writerows(cent_rows)
        print(f"[+] Centralizado CSV actualizado: {centralized_file} ({len(cent_rows):,} filas)")

        # Guardar Excel Centralizado
        cent_xlsx = centralized_file.replace(".csv", ".xlsx")
        export_excel(cent_rows, cent_headers, cent_xlsx, sheet_title="Prospectos Centralizados")

    print(f"\n[+] Proceso completo finalizado en {time.time() - t0:.2f} segundos.")


def export_excel(rows, headers, filename, sheet_title="Datos"):
    if not HAS_OPENPYXL:
        return
    print(f"[*] Construyendo Excel formateado: {filename}...")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(headers)

    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    border_thin = Border(
        left=Side(style='thin', color='E0E0E0'),
        right=Side(style='thin', color='E0E0E0'),
        top=Side(style='thin', color='E0E0E0'),
        bottom=Side(style='thin', color='E0E0E0')
    )

    for col_idx in range(1, len(headers) + 1):
        c = ws.cell(row=1, column=col_idx)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row_idx, r_dict in enumerate(rows, 2):
        for col_idx, col_name in enumerate(headers, 1):
            val = r_dict.get(col_name, "")
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = border_thin
            cell.font = Font(name="Calibri", size=10)

            # Destacar teléfonos, emails y ubicación
            if col_name in ["Teléfono / Celular (RNP)", "Email de Contacto (RNP)"]:
                if val and val != "No registra":
                    cell.font = Font(name="Calibri", size=10, bold=True, color="1F4E78")

            elif col_name in ["Departamento", "Provincia", "Distrito"]:
                if val and val != "No registra":
                    cell.font = Font(name="Calibri", size=10, color="003366")
                cell.alignment = Alignment(horizontal="center")

            elif col_name in ["Estado SUNAT", "Condición Domicilio", "Habilitado RNP", "Apto para Contratar"]:
                cell.alignment = Alignment(horizontal="center")

            elif col_name in ["Monto Referencial (S/.)", "Monto Total Concursado (S/.)"]:
                try:
                    num_val = float(val) if val else 0.0
                    cell.value = num_val
                    cell.number_format = '"S/." #,##0.00'
                except (ValueError, TypeError):
                    pass

            elif col_name == "Ficha RNP (Web)" and str(val).startswith("http"):
                cell.value = f'=HYPERLINK("{val}", "Ver Ficha RNP")'
                cell.font = Font(name="Calibri", size=10, color="0563C1", underline="single")
                cell.alignment = Alignment(horizontal="center")

            elif col_name == "Ver Proceso (Portal OECE)" and "http" in str(val) and not str(val).startswith("="):
                cell.value = f'=HYPERLINK("{val}", "Ver en OECE")'
                cell.font = Font(name="Calibri", size=10, color="0563C1", underline="single")
                cell.alignment = Alignment(horizontal="center")

            elif col_name == "URL Bases (PDF Original)" and "http" in str(val) and not str(val).startswith("="):
                cell.value = f'=HYPERLINK("{val}", "Descargar Bases")'
                cell.font = Font(name="Calibri", size=10, color="0563C1", underline="single")
                cell.alignment = Alignment(horizontal="center")

            elif col_name in ["RUC", "Condición", "Objeto de Contratación", "Fecha Convocatoria", "Días Transcurridos", "Categoría"]:
                cell.alignment = Alignment(horizontal="center")

    # Auto-ancho
    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col[:120]:
            v_str = str(cell.value or "")
            if v_str.startswith("=HYPERLINK"):
                v_str = "Descargar Bases"
            if len(v_str) > max_len:
                max_len = len(v_str)
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 11), 38)

    ws.freeze_panes = "A2"

    try:
        wb.save(filename)
        print(f"[+] Archivo Excel guardado: {filename}")
    except PermissionError:
        fallback = filename.replace(".xlsx", f"_{int(time.time()) % 10000}.xlsx")
        wb.save(fallback)
        print(f"[!] Archivo en uso. Guardado como: {fallback}")


def main():
    parser = argparse.ArgumentParser(description="Enriquecedor de Ubicación SUNAT (Departamento, Provincia, Distrito)")
    parser.add_argument("--directorio", type=str, default="prospectos_seace_2026_07_3887_DIRECTORIO_RUC.csv", help="Archivo CSV del Directorio")
    parser.add_argument("--centralizado", type=str, default="prospectos_seace_2026_07_3887_CENTRALIZADO.csv", help="Archivo CSV Centralizado")
    parser.add_argument("--limit", type=int, default=None, help="Límite para pruebas")
    parser.add_argument("--workers", type=int, default=60, help="Hilos paralelos (default: 60)")

    args = parser.parse_args()
    enrich_locations(directory_file=args.directorio, centralized_file=args.centralizado, limit=args.limit, workers=args.workers)


if __name__ == "__main__":
    main()
