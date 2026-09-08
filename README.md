# Scraper SEACE (HTTP / browserless)

Extractor de licitaciones públicas de **SEACE PROD2** (`prod2.seace.gob.pe`) vía requests + BeautifulSoup, sin Playwright.

## Qué hace

1. **Listado** — búsqueda y paginación PrimeFaces/JSF (formulario `tbBuscador:idFormBuscarProceso`).
2. **Sync incremental** — modos `backfill` / `sync` con SQLite (`seace.db`).
3. **Ficha de selección** — POST completo → redirect a `fichaSeleccion.xhtml`.
4. **Documentos** — descarga Bases y Documentos de Presentación de Propuestas vía Alfresco (`downloadDoc` JSONP + ticket).

## Setup

```bash
python -m venv venv
# Windows
.\venv\Scripts\activate
pip install -r requirements.txt
```

Coloca un token fresco de reCAPTCHA en `token.txt` (campo `tokenBusProSel`). El token expira; hay que renovarlo periódicamente.

## Uso

Edita la configuración al inicio de `main.py` (`MODO`, fechas, objeto contractual, flags de fichas) y ejecuta:

```bash
python main.py
```

## Notas

- No subas `token.txt` al repositorio.
- `venv/` y `__pycache__/` están en `.gitignore`.
- Los PDFs en `fichas/archivos/` son ejemplos de corridas de prueba.
