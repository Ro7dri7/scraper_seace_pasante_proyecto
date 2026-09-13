"""
Centralizador Maestro de Licitaciones y Contactos RNP / SEACE
Une y centraliza la base de datos detallada de licitaciones (SEACE) con la
información enriquecida de contacto y perfil de proveedores (RNP / OECE).

Inserta de forma ordenada y alineada por RUC:
- Teléfono / Celular (RNP)
- Email de Contacto (RNP)
- Habilitado RNP
- Apto para Contratar
- Tasa de Éxito (Win Rate)
- Ficha RNP (Web)
"""

import os
import csv
import time
import argparse
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


def load_directory_map(dir_csv="prospectos_seace_2026_07_3887_DIRECTORIO_RUC.csv", cache_json="cache_contactos_rnp.json"):
    """Carga los datos de contacto y estadísticas indexados por RUC."""
    directory = {}

    # 1. Intentar cargar desde el CSV de directorio
    if os.path.exists(dir_csv):
        print(f"[*] Cargando directorio RNP desde: {dir_csv}...")
        try:
            with open(dir_csv, mode="r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ruc = row.get("RUC", "").strip()
                    if ruc:
                        directory[ruc] = {
                            "telefono": row.get("Teléfono / Celular (RNP)", "No registra"),
                            "email": row.get("Email de Contacto (RNP)", "No registra"),
                            "habilitado": row.get("Habilitado RNP", "No"),
                            "apto": row.get("Apto para Contratar", "No"),
                            "tasa_exito": row.get("Tasa de Éxito", "0.0%"),
                            "ficha_url": row.get("Ficha RNP (Web)", f"https://apps.oece.gob.pe/perfilprov-ui/ficha/{ruc}")
                        }
            print(f"[+] Proveedores cargados desde CSV de directorio: {len(directory):,}")
            return directory
        except Exception as e:
            print(f"[!] Advertencia al leer CSV: {e}")

    # 2. Fallback: Cargar desde cache JSON si no hay CSV
    if os.path.exists(cache_json):
        print(f"[*] Cargando desde caché JSON: {cache_json}...")
        try:
            import json
            with open(cache_json, mode="r", encoding="utf-8") as f:
                c_data = json.load(f)
                for ruc, info in c_data.items():
                    directory[ruc] = {
                        "telefono": info.get("telefono", "No registra"),
                        "email": info.get("email", "No registra"),
                        "habilitado": info.get("habilitado", "No"),
                        "apto": info.get("apto", "No"),
                        "tasa_exito": "N/D",
                        "ficha_url": info.get("ficha_url", f"https://apps.oece.gob.pe/perfilprov-ui/ficha/{ruc}")
                    }
            print(f"[+] Proveedores cargados desde JSON: {len(directory):,}")
        except Exception as e:
            print(f"[!] Advertencia al leer JSON: {e}")

    return directory


def centralize_data(input_file, directory_csv=None, output_file=None):
    t0 = time.time()
    print("=" * 75)
    print(" CENTRALIZADOR MAESTRO: LICITACIONES SEACE + CONTACTOS OFICIALES RNP")
    print("=" * 75)
    print(f" Archivo Base Licitaciones: {input_file}")

    if not os.path.exists(input_file):
        print(f"[!] Error: No se encontró el archivo '{input_file}'")
        return

    # Determinar ruta del directorio
    base_dir = os.path.splitext(input_file)[0]
    if not directory_csv:
        candidate_csv = f"{base_dir}_DIRECTORIO_RUC.csv"
        directory_csv = candidate_csv if os.path.exists(candidate_csv) else "prospectos_seace_2026_07_3887_DIRECTORIO_RUC.csv"

    # Cargar mapa de contactos RNP
    dir_map = load_directory_map(dir_csv=directory_csv)

    # Cargar archivo de licitaciones
    print(f"\n[*] Leyendo archivo base: {input_file}...")
    wb_in = openpyxl.load_workbook(input_file, read_only=True)
    ws_in = wb_in.active
    row_iter = ws_in.iter_rows(values_only=True)

    raw_headers = [str(c) if c is not None else "" for c in next(row_iter)]
    print(f"[+] Columnas originales ({len(raw_headers)}): {raw_headers[:5]}...")

    # Mapeo de índices de columnas originales
    col_idx = {h: i for i, h in enumerate(raw_headers)}

    # Definir estructura ordenada de columnas para el nuevo Excel Centralizado:
    # 1. Identificación y Condición de la Empresa
    # 2. Contactos Oficiales y Validación RNP (Nuevas)
    # 3. Detalle de la Licitación y Documentos SEACE
    ordered_headers = [
        "RUC",
        "Razón Social",
        "Condición",
        # --- NUEVAS COLUMNAS CENTRALIZADAS ---
        "Teléfono / Celular (RNP)",
        "Email de Contacto (RNP)",
        "Habilitado RNP",
        "Apto para Contratar",
        "Ficha RNP (Web)",
        # --- DATOS DE LA LICITACIÓN ---
        "Licitación",
        "Entidad Convocante",
        "Objeto de Contratación",
        "Objeto / Descripción",
        "Tipo Procedimiento",
        "Categoría",
        "Monto Referencial (S/.)",
        "Fecha Convocatoria",
        "Días Transcurridos",
        "Tipo Documento",
        "Ver Proceso (Portal OECE)",
        "URL Bases (PDF Original)",
        "OCID",
        "Requiere ISO"
    ]

    # Procesar filas
    centralized_rows = []
    enriched_count = 0
    total_rows = 0

    print("[*] Integrando y cruzando contactos oficiales por cada RUC...")
    for r_vals in row_iter:
        total_rows += 1
        ruc = str(r_vals[col_idx.get("RUC", 0)] or "").strip()
        
        # Buscar en directorio
        cont = dir_map.get(ruc, {
            "telefono": "No registra",
            "email": "No registra",
            "habilitado": "No",
            "apto": "No",
            "tasa_exito": "0.0%",
            "ficha_url": f"https://apps.oece.gob.pe/perfilprov-ui/ficha/{ruc}" if ruc else ""
        })

        if cont["telefono"] != "No registra":
            enriched_count += 1

        # Extraer montos numéricos limpios
        monto_raw = r_vals[col_idx.get("Monto Referencial (S/.)", 8)] if "Monto Referencial (S/.)" in col_idx else 0
        try:
            monto_val = float(monto_raw) if monto_raw is not None else 0.0
        except (ValueError, TypeError):
            monto_val = 0.0

        dias_raw = r_vals[col_idx.get("Días Transcurridos", 10)] if "Días Transcurridos" in col_idx else ""
        try:
            dias_val = int(dias_raw) if dias_raw is not None else ""
        except (ValueError, TypeError):
            dias_val = str(dias_raw or "")

        row_dict = {
            "RUC": ruc,
            "Razón Social": r_vals[col_idx.get("Razón Social", 1)] if "Razón Social" in col_idx else "",
            "Condición": r_vals[col_idx.get("Condición", 2)] if "Condición" in col_idx else "",
            # Nuevos datos integrados
            "Teléfono / Celular (RNP)": cont["telefono"],
            "Email de Contacto (RNP)": cont["email"],
            "Habilitado RNP": cont["habilitado"],
            "Apto para Contratar": cont["apto"],
            "Ficha RNP (Web)": cont["ficha_url"],
            # Licitación
            "Licitación": r_vals[col_idx.get("Licitación", 3)] if "Licitación" in col_idx else "",
            "Entidad Convocante": r_vals[col_idx.get("Entidad Convocante", 4)] if "Entidad Convocante" in col_idx else "",
            "Objeto de Contratación": r_vals[col_idx["Objeto de Contratación"]] if ("Objeto de Contratación" in col_idx and col_idx["Objeto de Contratación"] < len(r_vals)) else "",
            "Objeto / Descripción": r_vals[col_idx.get("Objeto / Descripción", 5)] if "Objeto / Descripción" in col_idx else "",
            "Tipo Procedimiento": r_vals[col_idx.get("Tipo Procedimiento", 6)] if "Tipo Procedimiento" in col_idx else "",
            "Categoría": r_vals[col_idx.get("Categoría", 7)] if "Categoría" in col_idx else "",
            "Monto Referencial (S/.)": monto_val,
            "Fecha Convocatoria": r_vals[col_idx.get("Fecha Convocatoria", 9)] if "Fecha Convocatoria" in col_idx else "",
            "Días Transcurridos": dias_val,
            "Tipo Documento": r_vals[col_idx.get("Tipo Documento", 11)] if "Tipo Documento" in col_idx else "",
            "Ver Proceso (Portal OECE)": r_vals[col_idx.get("Ver Proceso (Portal OECE)", 12)] if "Ver Proceso (Portal OECE)" in col_idx else "",
            "URL Bases (PDF Original)": r_vals[col_idx.get("URL Bases (PDF Original)", 13)] if "URL Bases (PDF Original)" in col_idx else "",
            "OCID": r_vals[col_idx.get("OCID", 14)] if "OCID" in col_idx else "",
            "Requiere ISO": r_vals[col_idx.get("Requiere ISO", 15)] if "Requiere ISO" in col_idx else "Pendiente de Análisis IA"
        }
        centralized_rows.append(row_dict)

    # Ordenar por Fecha Convocatoria descendente (las más recientes arriba)
    centralized_rows.sort(key=lambda x: (str(x.get("Fecha Convocatoria") or ""), str(x.get("Licitación") or "")), reverse=True)

    print(f"[+] Total de filas procesadas: {total_rows:,}")
    print(f"[+] Filas con Teléfono Oficial RNP: {enriched_count:,} ({(enriched_count/total_rows)*100:.1f}%)")

    # Nombres de salida
    if not output_file:
        output_xlsx = f"{base_dir}_CENTRALIZADO.xlsx"
        output_csv = f"{base_dir}_CENTRALIZADO.csv"
    else:
        output_xlsx = output_file if output_file.endswith(".xlsx") else f"{output_file}.xlsx"
        output_csv = output_xlsx.replace(".xlsx", ".csv")

    # 1. Exportar CSV
    print(f"\n[*] Exportando archivo CSV: {output_csv}...")
    with open(output_csv, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ordered_headers)
        writer.writeheader()
        writer.writerows(centralized_rows)
    print(f"[+] CSV exportado correctamente ({os.path.getsize(output_csv):,} bytes).")

    # 2. Exportar Excel con estilos y formato corporativo
    print(f"[*] Construyendo Excel Centralizado formateado: {output_xlsx}...")
    wb_out = openpyxl.Workbook()
    ws_out = wb_out.active
    ws_out.title = "Prospectos Centralizados"
    ws_out.append(ordered_headers)

    # Estilos
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    border_thin = Border(
        left=Side(style='thin', color='E0E0E0'),
        right=Side(style='thin', color='E0E0E0'),
        top=Side(style='thin', color='E0E0E0'),
        bottom=Side(style='thin', color='E0E0E0')
    )

    for col_idx_val in range(1, len(ordered_headers) + 1):
        cell = ws_out.cell(row=1, column=col_idx_val)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Escritura de celdas con estilos aplicados
    for row_idx, r_data in enumerate(centralized_rows, 2):
        for col_idx_num, col_name in enumerate(ordered_headers, 1):
            val = r_data.get(col_name, "")
            c = ws_out.cell(row=row_idx, column=col_idx_num, value=val)
            c.border = border_thin
            c.font = Font(name="Calibri", size=10)

            # Destacar teléfonos y correos
            if col_name in ["Teléfono / Celular (RNP)", "Email de Contacto (RNP)"]:
                if val and val != "No registra":
                    c.font = Font(name="Calibri", size=10, bold=True, color="1F4E78")

            # Formato de moneda
            elif col_name == "Monto Referencial (S/.)" and isinstance(val, (int, float)):
                c.number_format = '"S/." #,##0.00'

            # Hipervínculo Ficha RNP
            elif col_name == "Ficha RNP (Web)" and str(val).startswith("http"):
                c.value = f'=HYPERLINK("{val}", "Ver Ficha RNP")'
                c.font = Font(name="Calibri", size=10, color="0563C1", underline="single")
                c.alignment = Alignment(horizontal="center")

            # Hipervínculos existentes
            elif col_name == "Ver Proceso (Portal OECE)" and "http" in str(val):
                if not str(val).startswith("="):
                    c.value = f'=HYPERLINK("{val}", "Ver en OECE")'
                c.font = Font(name="Calibri", size=10, color="0563C1", underline="single")
                c.alignment = Alignment(horizontal="center")

            elif col_name == "URL Bases (PDF Original)" and "http" in str(val):
                if not str(val).startswith("="):
                    c.value = f'=HYPERLINK("{val}", "Descargar Bases")'
                c.font = Font(name="Calibri", size=10, color="0563C1", underline="single")
                c.alignment = Alignment(horizontal="center")

            # Centrar campos clave
            elif col_name in ["RUC", "Condición", "Objeto de Contratación", "Habilitado RNP", "Apto para Contratar", "Fecha Convocatoria", "Días Transcurridos", "Categoría"]:
                c.alignment = Alignment(horizontal="center")

    # Auto-ancho muestreando las primeras 100 filas para velocidad
    for col in ws_out.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col[:100]:
            v_str = str(cell.value or "")
            if v_str.startswith("=HYPERLINK"):
                v_str = "Descargar Bases"
            if len(v_str) > max_len:
                max_len = len(v_str)
        ws_out.column_dimensions[col_letter].width = min(max(max_len + 3, 11), 38)

    # Congelar fila de cabecera
    ws_out.freeze_panes = "A2"

    try:
        wb_out.save(output_xlsx)
        print(f"[+] Archivo Excel Centralizado guardado con éxito: {output_xlsx}")
    except PermissionError:
        fallback = output_xlsx.replace(".xlsx", f"_{int(time.time()) % 10000}.xlsx")
        wb_out.save(fallback)
        print(f"[!] Archivo en uso. Se guardó copia como: {fallback}")

    print(f"[+] Operación completada en {time.time() - t0:.2f} segundos.")


def main():
    parser = argparse.ArgumentParser(description="Centralizador de Licitaciones SEACE con Contactos Oficiales RNP")
    parser.add_argument("--input", type=str, default="prospectos_seace_2026_07_3887.xlsx", help="Archivo base de licitaciones")
    parser.add_argument("--directory", type=str, default=None, help="Archivo CSV del directorio de empresas RNP")
    parser.add_argument("--output", type=str, default=None, help="Nombre del archivo de salida")

    args = parser.parse_args()
    centralize_data(args.input, directory_csv=args.directory, output_file=args.output)


if __name__ == "__main__":
    main()
