# LicitApp — motor unificado OECE + SEACE (PROD2 + PROD6)

Todo corre en este directorio: extracción masiva OECE (API), enriquecimiento RNP/SUNAT, delta SEACE PROD2 con 2Captcha, delta SEACE PROD6 vía API REST, deduplicación por nomenclatura y sync a Supabase.

## Las 3 fuentes

| Orden | Fuente | Módulo | Acceso |
|---|---|---|---|
| 1 | OECE (bulk histórico) | `extractor_seace_oece.py` / `pipeline_seace.py` | Paquete CSV mensual |
| 2 | SEACE PROD2 (Licitaciones Mayores) | `scraper_prod2.py` | JSF + reCAPTCHA (2Captcha) |
| 3 | SEACE PROD6 (Compras Menores ≤ 8 UIT) | `scraper_prod6.py` | API REST JSON, sin CAPTCHA |

La llave de deduplicación común entre las tres es la **nomenclatura normalizada** (`nomenclatura_norm`).

## Estrategia on-demand

El scraper **no descarga PDFs ni ZIPs**. De cada documento de la ficha se guardan en `documentos_proceso` el `file_code` y la `url_descarga` de Alfresco (resolver JSONP, que entrega un ticket fresco al invocarse). El binario se procesa con IA más adelante, solo cuando un usuario desbloquea la licitación.

Ese estado vive en `convocatorias.requiere_iso`, que nace en `'Bloqueado'` por DEFAULT. Los scrapers nunca escriben esa columna, así que un re-upsert no pisa un desbloqueo ya concedido. El JSON del análisis ISO lo escribe únicamente `api_desbloqueo.py` cuando el usuario gasta un crédito.

## Setup

```powershell
cd C:\scraper_licitigo\scraper_seace_pasante_proyecto
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Completa en `.env`:
- `TWOCAPTCHA_API_KEY` (solo PROD2)
- `SUPABASE_URL` + `SUPABASE_SECRET_KEY`
- `LLM_API_KEY`, `LLM_MODEL` y opcionalmente `LLM_BASE_URL` (OpenRouter, KeyAI u OpenAI oficial)

Ejecuta `schema_supabase.sql` en el SQL Editor de Supabase. Es idempotente: agrega `requiere_iso`, `items_proceso` y los campos de PROD6 sobre tablas ya existentes.

## Uso

Flujo maestro completo (OECE → PROD2 → PROD6), con conteo de procesos nuevos por fuente:

```powershell
python main.py --year 2026 --month 09
```

Flags útiles del orquestador:

```powershell
python main.py --skip-oece                 # solo los deltas SEACE
python main.py --sin-enriquecimiento       # OECE sin RNP / SUNAT
python main.py --max-fichas-prod2 3 --max-detalles-prod6 20   # corrida de prueba
```

Cada módulo también corre suelto:

```powershell
python pipeline_seace.py --year 2026 --month 09 --skip-seace   # solo OECE
python scraper_prod2.py --desde 01/09/2026                     # solo delta mayor
python scraper_prod6.py --anio 2026                            # solo delta menor
python scraper_prod6.py --dry-run --max-detalles 3             # PROD6 sin escribir
```

## PROD6 en detalle

1. **Búsqueda** — `GET /contrataciones/buscador?anio&lista_estado_contrato=2&orden=2&page&page_size`, paginado con `while` hasta agotar `pageable.totalElements`.
2. **Deduplicación** — `nroDescripcion` normalizado (mayúsculas, sin espacios) contra las nomenclaturas ya cargadas en Supabase. Tras `PROD6_PAGINAS_SIN_NUEVOS` páginas seguidas sin novedades corta el delta (`--full` lo desactiva).
3. **Detalle** — `GET /contrataciones/listar-completo?id_contrato={id}` en paralelo (`PROD6_WORKERS`).
4. **Upsert** — `convocatorias` con `fuente='PROD6'` + `items_proceso` con `uitContratoItemProjectionList`.

`montoContrato` no viene en el detalle mientras el proceso está en cotización; el mapeo lo toma si aparece y si no deja `monto` vacío.

## Desbloqueo ISO (on-demand)

Cuando el frontend / n8n cobra un crédito, llama a este microservicio. Descarga el PDF en memoria (nunca a disco), recorta ~1000 palabras de las secciones de calificación/ISO y pregunta al LLM vía el SDK oficial `openai` (`LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`), compatible con OpenRouter, KeyAI u OpenAI.

```powershell
pip install -r requirements.txt
python api_desbloqueo.py
```

```http
POST /api/v1/analizar-iso
Content-Type: application/json

{"nomenclatura_norm": "LP-1-2026-UE001"}
```

Respuesta `200`:

```json
{
  "nomenclatura_norm": "LP-1-2026-UE001",
  "documento": {"categoria": "bases_integradas", "url_descarga": "..."},
  "analisis": {
    "requiere_iso": true,
    "normas_identificadas": ["ISO 9001"],
    "condicion": "Obligatorio",
    "resumen_requisito": "..."
  },
  "paginas_analizadas": [42, 43],
  "palabras_enviadas": 380,
  "llm_invocado": true
}
```

Ese mismo JSON (más `analizado_at`) queda persistido en `convocatorias.requiere_iso`. Errores: `404` sin documento, `422` si no es PDF/ZIP, `429` rate limit del LLM, `502`/`504` caída o timeout del proveedor, `500` extracción o configuración.

## Tablas en Supabase

- `convocatorias` — llave `nomenclatura_norm`, con `fuente`, `requiere_iso`, `id_contrato`, `estado`, `fecha_fin_cotizacion`.
- `documentos_proceso` — `file_code` + `url_descarga` (sin rutas locales ni bytes).
- `items_proceso` — ítems de compras menores (`codigo_cubso`, `cantidad`, `unidad_medida`, …).
- `proveedores` — contactos RNP/SUNAT.

## Notas

- No subas `.env`, `token.txt` ni `handoff_state.json`.
- `OECE_HANDOFF_FILE=handoff_state.json` es la posta OECE → PROD2.
- `SEACE_GUARDAR_FICHA_HTML=true` solo para depurar el parser de fichas.
