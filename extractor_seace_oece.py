"""
Este script automatiza la extracción de licitaciones públicas y la lista completa
de empresas postulantes (con su RUC, razón social, condición de ganador/postulante,
y enlaces directos funcionales tanto al portal de OECE como a las bases documentales).
Optimizado para máxima velocidad de ejecución (procesamiento relacional en memoria).

"""

import os
import io
import re
import ssl
import csv
import json
import time
import zipfile
import argparse
import datetime
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from lib_nomenclatura import extraer_nomenclatura
from lib_embudo import clasificar_estado_oece

# Desactivar verificación estricta de SSL si hay problemas con certificados gubernamentales
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

BASE_API = "https://contratacionesabiertas.oece.gob.pe/api/v1"


def get_latest_available_package():
    """Consulta la API de OECE para obtener el año y mes del dump más reciente."""
    url = f"{BASE_API}/files?page=1&paginateBy=5&format=json"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            results = data.get("results", [])
            for item in results:
                if item.get("source") == "seace_v3":
                    return item.get("year"), item.get("month"), item.get("files", {}).get("csv")
    except Exception as e:
        print(f"[!] Error al consultar archivos recientes: {e}")
    now = datetime.datetime.now()
    prev_month = now.month - 1 or 12
    prev_year = now.year if now.month > 1 else now.year - 1
    return str(prev_year), f"{prev_month:02d}", None


def download_zip(year, month):
    """Descarga el paquete CSV comprimido del mes especificado."""
    url = f"{BASE_API}/file/seace_v3/csv/{year}/{month}/"
    print(f"[*] Descargando paquete masivo OECE: {year}-{month}...")
    print(f"    URL: {url}")
    
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=60) as resp:
        content = resp.read()
        print(f"[+] Descarga completada. Tamaño: {len(content) / (1024*1024):.2f} MB")
        return zipfile.ZipFile(io.BytesIO(content))


def clean_ruc(raw_id):
    """Limpia el prefijo PE-RUC- para dejar solo los 11 dígitos del RUC."""
    if not raw_id:
        return ""
    return str(raw_id).replace("PE-RUC-", "").replace("RUC-", "").strip()


def get_document_score(title, doc_type):
    """
    Puntúa el documento para priorizar estrictamente las Bases del proceso:
    Prioridad 1: Bases Integradas (las definitivas con todos los requisitos)
    Prioridad 2: Bases Administrativas / Bases
    Prioridad 0: Excluir actas, propuestas, reportes de ofertas, subsanaciones
    """
    t = (title or "").lower()
    dt = (doc_type or "").lower()
    combined = f"{t} {dt}"

    # Excluir documentos no relevantes
    exclusiones = [
        "presentacion de propuesta", "presentación de propuesta", "reporte",
        "acta de evaluacion", "acta de evaluación", "buena pro", "resumen ejecutivo",
        "absolucion", "absolución", "subsanacion", "subsanación", "oferta"
    ]
    if any(ex in combined for ex in exclusiones):
        return 0

    if "bases integradas" in combined:
        return 100
    if "bases administrativas" in combined or "bases" in combined:
        return 50
    if "términos de referencia" in combined or "tdr" in combined or "especificaciones técnicas" in combined:
        return 30
    if "biddingdocuments" in dt:
        return 20

    return 0


def map_objeto_contratacion(main_cat, proc_details, title, desc):
    """
    Mapea la categoría a la clasificación oficial de la Ley de Contrataciones del Estado:
    - Bienes
    - Servicios
    - Consultoría de Obra
    - Consultoría
    - Obras
    """
    combined = f"{proc_details} {title} {desc}".lower()
    cat = (main_cat or "").lower()

    if "consultoría de obra" in combined or "consultoria de obra" in combined:
        return "Consultoría de Obra"
    elif "consultoría" in combined or "consultoria" in combined or "consultor" in combined:
        return "Consultoría"
    elif cat == "works" or "ejecución de obra" in combined or "obra" in combined:
        return "Obras"
    elif cat == "goods":
        return "Bienes"
    elif cat == "services":
        return "Servicios"

    mapping = {
        "goods": "Bienes",
        "services": "Servicios",
        "works": "Obras",
        "consultingservices": "Consultoría"
    }
    return mapping.get(cat, cat.capitalize() or "No especificado")


def parse_oece_data(z, min_days_old=0, max_records=None, target_month=None):
    """
    Lee y cruza las tablas del archivo ZIP de OECE en memoria:
    - records.csv (Licitaciones y Entidades)
    - com_ten_tenderers.csv (Postulantes con RUC)
    - com_awa_suppliers.csv (Ganadores/Adjudicatarios)
    - com_ten_documents.csv (Enlaces a bases administrativas)
    """
    print("[*] Procesando tablas relacionales en memoria...")

    # 1. Ganadores (Adjudicatarios) y Montos Adjudicados
    print("    -> Indexando adjudicatarios y montos de adjudicación...")
    winners = set()
    if "com_awa_suppliers.csv" in z.namelist():
        with z.open("com_awa_suppliers.csv") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8', errors='ignore'))
            for row in reader:
                ocid = row.get("ocid")
                raw_id = row.get("compiledRelease/awards/suppliers/id") or row.get("compiledRelease/awards/0/suppliers/0/id")
                ruc = clean_ruc(raw_id)
                if ocid and ruc:
                    winners.add((ocid, ruc))

    award_amounts = {}
    if "com_awards.csv" in z.namelist():
        with z.open("com_awards.csv") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8', errors='ignore'))
            for row in reader:
                ocid = row.get("ocid")
                amt_str = row.get("compiledRelease/awards/0/value/amount")
                aid = row.get("compiledRelease/awards/0/id", "")
                if ocid and amt_str:
                    try:
                        amt_val = float(amt_str)
                        if amt_val > 0:
                            award_amounts[ocid] = amt_val
                            parts = aid.split("-")
                            if len(parts) > 1:
                                award_amounts[(ocid, clean_ruc(parts[-1]))] = amt_val
                    except (ValueError, TypeError):
                        pass

    # 2. Documentos y Bases en PDF
    print("    -> Indexando documentos y bases en PDF (filtrando actas/reportes)...")
    bases_docs = {}
    if "com_ten_documents.csv" in z.namelist():
        with z.open("com_ten_documents.csv") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8', errors='ignore'))
            for row in reader:
                ocid = row.get("ocid")
                url = row.get("compiledRelease/tender/documents/url") or row.get("compiledRelease/tender/documents/0/url", "")
                title = row.get("compiledRelease/tender/documents/title") or row.get("compiledRelease/tender/documents/0/title", "")
                doc_type = row.get("compiledRelease/tender/documents/documentType") or row.get("compiledRelease/tender/documents/0/documentType", "")

                if not ocid or not url:
                    continue

                score = get_document_score(title, doc_type)
                if score == 0:
                    continue

                current = bases_docs.get(ocid)
                if not current or score > current["score"]:
                    bases_docs[ocid] = {
                        "url": url,
                        "title": title or "Bases Administrativas",
                        "score": score
                    }

    # 3. Postulantes por Licitación
    print("    -> Indexando empresas postulantes...")
    tenderers_by_ocid = defaultdict(list)
    seen_tenderers = set()
    if "com_ten_tenderers.csv" in z.namelist():
        with z.open("com_ten_tenderers.csv") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8', errors='ignore'))
            for row in reader:
                ocid = row.get("ocid")
                raw_id = row.get("compiledRelease/tender/tenderers/id") or row.get("compiledRelease/tender/tenderers/0/id")
                tenderer_id = clean_ruc(raw_id)
                tenderer_name = row.get("compiledRelease/tender/tenderers/name") or row.get("compiledRelease/tender/tenderers/0/name", "No especificado")

                if ocid and tenderer_id:
                    key = (ocid, tenderer_id)
                    if key not in seen_tenderers:
                        seen_tenderers.add(key)
                        tenderers_by_ocid[ocid].append({
                            "ruc": tenderer_id,
                            "razon_social": tenderer_name.strip()
                        })

    # 4. Procesar Licitaciones desde records.csv
    print("    -> Cruzando con licitaciones y entidades convocantes...")
    leads = []
    stubs_bloqueados = []
    now = datetime.datetime.now()
    fecha_max = ""
    fecha_max_iso = ""
    fecha_min = ""
    nomenclaturas_norm = set()
    procesos_vistos = 0
    procesos_bloqueados = 0
    fecha_max_dt = None
    ocids_con_ganador = {ocid for ocid, _ruc in winners}

    with z.open("records.csv") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8', errors='ignore'))
        for row in reader:
            ocid = row.get("ocid")
            if not ocid:
                continue

            pub_date_str = row.get("compiledRelease/tender/datePublished") or row.get("compiledRelease/date")
            days_old = None
            pub_date_clean = ""
            pub_dt = None
            if pub_date_str:
                try:
                    pub_dt = datetime.datetime.fromisoformat(pub_date_str)
                    days_old = (now.date() - pub_dt.date()).days
                    pub_date_clean = pub_dt.strftime("%Y-%m-%d")
                except Exception:
                    pub_date_clean = pub_date_str[:10]
                    try:
                        d_part = datetime.date.fromisoformat(pub_date_clean)
                        days_old = (now.date() - d_part).days
                        pub_dt = datetime.datetime.combine(d_part, datetime.time.min)
                    except Exception:
                        days_old = ""

            if target_month and pub_date_clean and not pub_date_clean.startswith(target_month):
                continue

            titulo = row.get("compiledRelease/tender/title", "")
            tender_id = row.get("compiledRelease/tender/id") or row.get("compiledRelease/tender/id/0", "")
            nom_raw, nom_norm = extraer_nomenclatura(tender_id, titulo, ocid)
            if nom_norm:
                nomenclaturas_norm.add(nom_norm)
            procesos_vistos += 1
            if pub_date_clean:
                if not fecha_max or pub_date_clean > fecha_max:
                    fecha_max = pub_date_clean
                if not fecha_min or pub_date_clean < fecha_min:
                    fecha_min = pub_date_clean
            if pub_dt and (fecha_max_dt is None or pub_dt > fecha_max_dt):
                fecha_max_dt = pub_dt
                fecha_max_iso = pub_dt.isoformat()

            postulantes = tenderers_by_ocid.get(ocid, [])
            status_raw = (
                row.get("compiledRelease/tender/status")
                or row.get("compiledRelease/tender/status/0")
                or ""
            )
            estado_es, bloqueada = clasificar_estado_oece(
                status_raw, tiene_ganador=(ocid in ocids_con_ganador)
            )
            if bloqueada:
                procesos_bloqueados += 1

            if not postulantes:
                # Igual persistimos la cabecera si el proceso ya cerró, para
                # que PROD2/PROD6 jamás gasten proxy/captcha en él.
                if bloqueada and nom_norm:
                    stubs_bloqueados.append({
                        "RUC": "",
                        "Nomenclatura": nom_raw,
                        "Nomenclatura Norma": nom_norm,
                        "Fuente": "oece",
                        "Licitación": titulo,
                        "Entidad Convocante": row.get("compiledRelease/buyer/name", ""),
                        "Fecha Convocatoria": pub_date_clean,
                        "OCID": ocid,
                        "Estado": estado_es,
                        "Bloqueada": True,
                    })
                continue

            if min_days_old > 0 and days_old != "" and days_old is not None and days_old < min_days_old:
                continue

            descripcion = row.get("compiledRelease/tender/description", "")
            entidad = row.get("compiledRelease/buyer/name", "")
            tipo_proc = row.get("compiledRelease/tender/procurementMethodDetails", "")
            categoria_raw = row.get("compiledRelease/tender/mainProcurementCategory", "")
            objeto_contratacion = map_objeto_contratacion(categoria_raw, tipo_proc, titulo, descripcion)
            cat_map = {
                "goods": "Bienes",
                "services": "Servicios",
                "works": "Obras",
                "consultingservices": "Consultoría",
                "consulting services": "Consultoría"
            }
            categoria = cat_map.get((categoria_raw or "").lower(), categoria_raw.capitalize() if categoria_raw else "No especificado")
            monto_pen = row.get("compiledRelease/tender/value/amount_PEN", "")
            base_monto = None
            if monto_pen and monto_pen != "None":
                try:
                    val = float(monto_pen)
                    if val > 0:
                        base_monto = val
                except (ValueError, TypeError):
                    pass

            doc_info = bases_docs.get(ocid, {})
            url_bases = doc_info.get("url", "")
            tipo_doc = doc_info.get("title", "No disponible")

            for p in postulantes:
                es_ganador = (ocid, p["ruc"]) in winners
                condicion = "Ganador (Adjudicado)" if es_ganador else "Postulante"

                monto_lead = base_monto
                if monto_lead is None or monto_lead == 0.0:
                    monto_lead = award_amounts.get((ocid, p["ruc"])) or award_amounts.get(ocid) or 0.0

                lead = {
                    "RUC": p["ruc"],
                    "Razón Social": p["razon_social"],
                    "Condición": condicion,
                    "Nomenclatura": nom_raw,
                    "Nomenclatura Norma": nom_norm,
                    "Fuente": "oece",
                    "Licitación": titulo,
                    "Entidad Convocante": entidad,
                    "Objeto de Contratación": objeto_contratacion,
                    "Objeto / Descripción": descripcion,
                    "Tipo Procedimiento": tipo_proc,
                    "Categoría": categoria,
                    "Monto Referencial (S/.)": float(monto_lead) if monto_lead else 0.0,
                    "Fecha Convocatoria": pub_date_clean,
                    "Días Transcurridos": days_old if days_old is not None else "",
                    "Tipo Documento": tipo_doc,
                    "Ver Proceso (Portal OECE)": f"https://contratacionesabiertas.oece.gob.pe/proceso/{ocid}",
                    "URL Bases (PDF Original)": url_bases,
                    "OCID": ocid,
                    "Estado": estado_es,
                    "Bloqueada": bloqueada,
                    "Requiere ISO": "Pendiente de Análisis IA"
                }
                leads.append(lead)

                if max_records and len(leads) >= max_records:
                    break
            
            if max_records and len(leads) >= max_records:
                break

    # Ordenar estrictamente por Fecha Convocatoria descendente (las más actuales arriba)
    leads.extend(stubs_bloqueados)
    leads.sort(key=lambda x: (str(x.get("Fecha Convocatoria") or ""), str(x.get("Licitación") or "")), reverse=True)

    print(f"[+] Cruce completado. Total de leads (postulantes) extraídos: {len(leads) - len(stubs_bloqueados)}")
    print(f"[+] Procesos OECE indexados (con o sin postulantes): {procesos_vistos}")
    print(f"[+] Procesos marcados bloqueada (Finalizada/Otorgada/Cancelada): {procesos_bloqueados}")
    print(f"[+] Cabeceras bloqueadas sin postulantes (stubs): {len(stubs_bloqueados)}")
    print(f"[+] fecha_max OECE (posta SEACE): {fecha_max or 'N/D'}")
    if leads:
        max_date = leads[0].get("Fecha Convocatoria")
        min_date = leads[-1].get("Fecha Convocatoria")
        print(f"[+] Rango de fechas en leads: Desde {min_date} hasta {max_date} (La fecha más actual está en la Fila 1)")
    meta = {
        "fecha_max": fecha_max,
        "fecha_max_iso": fecha_max_iso,
        "fecha_min": fecha_min,
        "nomenclaturas_norm": sorted(nomenclaturas_norm),
        "total_procesos": procesos_vistos,
        "total_leads": len(leads) - len(stubs_bloqueados),
        "total_bloqueados": procesos_bloqueados,
    }
    return leads, meta


def export_to_excel(leads, filename="postulantes_seace.xlsx"):
    """Exporta los leads a formato Excel (.xlsx) con estilo profesional y enlaces con fórmula nativa HYPERLINK."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = "Prospectos SEACE"

        headers = list(leads[0].keys()) if leads else []
        ws.append(headers)

        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        border_thin = Border(
            left=Side(style='thin', color='D9D9D9'),
            right=Side(style='thin', color='D9D9D9'),
            top=Side(style='thin', color='D9D9D9'),
            bottom=Side(style='thin', color='D9D9D9')
        )

        for col_num, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        for row_idx, lead in enumerate(leads, 2):
            for col_idx, col_name in enumerate(headers, 1):
                val = lead.get(col_name, "")
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.border = border_thin
                cell.font = Font(name="Calibri", size=10)

                if col_name == "Condición":
                    if "Ganador" in str(val):
                        cell.fill = PatternFill(start_color="D9EAD3", end_color="D9EAD3", fill_type="solid")
                        cell.font = Font(name="Calibri", size=10, bold=True, color="274E13")
                    else:
                        cell.fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
                        cell.font = Font(name="Calibri", size=10, color="7F6000")
                elif col_name == "Monto Referencial (S/.)" and isinstance(val, (int, float)):
                    cell.number_format = '"S/." #,##0.00'
                elif col_name == "Ver Proceso (Portal OECE)" and str(val).startswith("http"):
                    # Fórmula nativa de Excel para garantizar hipervínculo activo y clickeable
                    cell.value = f'=HYPERLINK("{val}", "Ver en OECE")'
                    cell.font = Font(name="Calibri", size=10, color="0563C1", underline="single")
                    cell.alignment = Alignment(horizontal="center")
                elif col_name == "URL Bases (PDF Original)" and str(val).startswith("http"):
                    cell.value = f'=HYPERLINK("{val}", "Descargar Bases")'
                    cell.font = Font(name="Calibri", size=10, color="0563C1", underline="single")
                    cell.alignment = Alignment(horizontal="center")
                elif col_name in ["RUC", "Fecha Convocatoria", "Días Transcurridos", "Requiere ISO"]:
                    cell.alignment = Alignment(horizontal="center")

        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                val_str = str(cell.value or '')
                if val_str.startswith("=HYPERLINK"):
                    val_str = "Ver en OECE"
                if len(val_str) > max_len:
                    max_len = len(val_str)
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 45)

        try:
            wb.save(filename)
            print(f"[+] Archivo Excel generado con éxito: {filename}")
        except PermissionError:
            fallback = filename.replace(".xlsx", f"_{int(time.time()) % 10000}.xlsx")
            wb.save(fallback)
            print(f"[!] Aviso: '{filename}' está abierto en Excel. Se guardó copia como: {fallback}")
        return True
    except ImportError:
        print("[!] openpyxl no está instalado. Se omite la generación de .xlsx nativo.")
        return False


def export_to_csv(leads, filename="postulantes_seace.csv"):
    """Exporta los leads a CSV con UTF-8 BOM para apertura nativa en Excel."""
    if not leads:
        print("[!] No hay datos para exportar.")
        return

    headers = list(leads[0].keys())
    try:
        with open(filename, mode='w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(leads)
        print(f"[+] Archivo CSV generado con éxito: {filename}")
    except PermissionError:
        fallback = filename.replace(".csv", f"_{int(time.time()) % 10000}.csv")
        with open(fallback, mode='w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(leads)
        print(f"[!] Aviso: '{filename}' está abierto. Se guardó copia como: {fallback}")


def main():
    parser = argparse.ArgumentParser(description="Extractor ultrarrápido de prospectos comerciales SEACE / OECE")
    parser.add_argument("--year", type=str, help="Año de datos (ej. 2026)")
    parser.add_argument("--month", type=str, help="Mes de datos con 2 dígitos (ej. 07)")
    parser.add_argument("--latest", action="store_true", help="Descargar automáticamente el mes más reciente")
    parser.add_argument("--min-days", type=int, default=30, help="Mínimo de días desde la convocatoria (default: 30)")
    parser.add_argument("--max-records", type=int, default=None, help="Límite máximo de prospectos a exportar")
    parser.add_argument("--output-prefix", type=str, default="prospectos_seace", help="Prefijo de los archivos de salida")

    args = parser.parse_args()

    start_time = datetime.datetime.now()

    if args.latest or not (args.year and args.month):
        print("[*] Obteniendo el período disponible más reciente de OECE...")
        year, month, _ = get_latest_available_package()
        print(f"[+] Período seleccionado: {year}-{month}")
    else:
        year = args.year
        month = f"{int(args.month):02d}"

    try:
        zip_obj = download_zip(year, month)
    except Exception as e:
        print(f"[!] Error al descargar el paquete {year}-{month}: {e}")
        return

    # 1. Parsear datos relacionales en memoria filtrando estrictamente al mes objetivo
    target_period = f"{year}-{month}"
    leads, meta = parse_oece_data(zip_obj, min_days_old=args.min_days, max_records=args.max_records, target_month=target_period)

    fecha_max = meta.get("fecha_max") or ""
    fecha_max_iso = meta.get("fecha_max_iso") or fecha_max
    handoff = {
        "fuente": "oece",
        "year": str(year),
        "month": str(month),
        "fecha_max": fecha_max,
        "fecha_max_iso": fecha_max_iso,
        "fecha_max_seace": "",
        "fecha_min": meta.get("fecha_min") or "",
        "nomenclaturas_norm": meta.get("nomenclaturas_norm") or [],
        "total_procesos": meta.get("total_procesos") or 0,
        "total_leads": meta.get("total_leads") or 0,
        "updated_at": datetime.datetime.now().isoformat(),
    }
    if fecha_max:
        try:
            d = datetime.datetime.strptime(fecha_max[:10], "%Y-%m-%d")
            handoff["fecha_max_seace"] = d.strftime("%d/%m/%Y")
        except ValueError:
            handoff["fecha_max_seace"] = fecha_max
    handoff_path = f"{args.output_prefix}_{year}_{month}_handoff.json"
    state_path = Path(__file__).with_name("handoff_state.json")
    payload = json.dumps(handoff, ensure_ascii=False, indent=2)
    with open(handoff_path, "w", encoding="utf-8") as hf:
        hf.write(payload)
    state_path.write_text(payload, encoding="utf-8")
    print(f"[+] Handoff OECE→SEACE: {handoff_path} (fecha_max={fecha_max_iso})")
    print(f"[+] handoff_state.json (posta canónica): {state_path}")

    if not leads:
        print("[!] No se encontraron prospectos con los filtros especificados.")
        return

    csv_file = f"{args.output_prefix}_{year}_{month}.csv"
    xlsx_file = f"{args.output_prefix}_{year}_{month}.xlsx"

    export_to_csv(leads, csv_file)
    export_to_excel(leads, xlsx_file)
    try:
        from supabase_sync import upsert_oece_leads
        print("[*] Subiendo OECE a Supabase (convocatorias + proveedores)...")
        print("[*] Antes del upsert se enriquece cada RUC con RNP + SUNAT (caché en disco).")
        upsert_oece_leads(leads)
    except Exception as e:
        print(f"[!] Supabase OECE: {e}")

    elapsed = (datetime.datetime.now() - start_time).total_seconds()
    unique_rucs = len(set(l["RUC"] for l in leads))
    tenders_count = len(set(l["OCID"] for l in leads))
    bases_count = sum(1 for l in leads if l["URL Bases (PDF Original)"])

    print("\n" + "="*65)
    print(" RESUMEN DE EXTRACCIÓN SEACE / OECE (ALTA VELOCIDAD)")
    print("="*65)
    print(f" Tiempo de ejecución          : {elapsed:.2f} segundos")
    print(f" Total Prospectos (Leads)     : {len(leads)}")
    print(f" Empresas Únicas (RUCs)       : {unique_rucs}")
    print(f" Licitaciones cubiertas       : {tenders_count}")
    print(f" fecha_max (posta SEACE)      : {fecha_max}")
    print(f" Nomenclaturas únicas OECE    : {len(handoff['nomenclaturas_norm'])}")
    print(f" Enlaces directos a Bases PDF : {bases_count} ({(bases_count/len(leads))*100:.1f}%)")
    print(f" Archivos generados           : {xlsx_file} y {csv_file}")
    print("="*65 + "\n")


if __name__ == "__main__":
    main()
