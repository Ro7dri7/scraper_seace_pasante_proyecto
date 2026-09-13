# Scraper SEACE + OECE (proyecto unificado)

Todo corre en este directorio: extracción masiva OECE (API), enriquecimiento RNP/SUNAT, delta SEACE con 2Captcha, deduplicación por nomenclatura y sync a Supabase.

## Qué incluye

1. **OECE** — descarga CSV mensual, leads/postores, handoff (`handoff_state.json`).
2. **RNP / SUNAT** — teléfonos, emails, ubicación fiscal.
3. **SEACE PROD2** — listado HTTP + reCAPTCHA vía 2Captcha + fichas/Alfresco.
4. **Dedup** — nomenclaturas OECE + las ya subidas a Supabase.
5. **Supabase** — upsert a `convocatorias`, `proveedores`, `documentos_proceso`.

## Setup

```powershell
cd C:\scraper_licitigo\scraper_seace_pasante_proyecto
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Completa en `.env`:
- `TWOCAPTCHA_API_KEY`
- `SUPABASE_URL` + `SUPABASE_SECRET_KEY`

Si las tablas de Supabase son stubs, ejecuta una vez `schema_supabase.sql` en el SQL Editor.

## Uso

Pipeline completo (OECE → enriquecimiento → delta SEACE):

```powershell
python pipeline_seace.py --year 2026 --month 09
```

Solo OECE (sin SEACE):

```powershell
python pipeline_seace.py --year 2026 --month 09 --skip-seace
```

Prueba SEACE con tope de fichas:

```powershell
python pipeline_seace.py --year 2026 --month 09 --max-fichas-seace 3
```

Solo delta SEACE (requiere `handoff_state.json` local):

```powershell
python main.py
```

## Notas

- No subas `.env`, `token.txt` ni `handoff_state.json`.
- `OECE_HANDOFF_FILE=handoff_state.json` apunta a este mismo proyecto.
- Ya no hace falta `C:\extraccion_oesce\pipeline`.
