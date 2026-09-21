#!/usr/bin/env bash
# Embudo Licity Go: OECE (sin proxy) → PROD6 (IPRoyal) → PROD2 (IPRoyal + 2Captcha)
set -u
cd "$(dirname "$0")"
ROOT="$(pwd)"

if [[ -f "$ROOT/venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/venv/bin/activate"
else
  echo "[-] No existe venv/bin/activate. Crea el entorno virtual antes de cron." >&2
  exit 1
fi

mkdir -p "$ROOT/logs"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
LOG="$ROOT/logs/pipeline_${STAMP}.log"
YEAR="$(date +%Y)"
MONTH="$(date +%m)"
HORAS="${EMBUDO_HORAS_RADAR:-72}"
PY="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"
PY="${PY:-python3}"

exec > >(tee -a "$LOG") 2>&1
echo "============================================================"
echo " Licity Go pipeline  ${STAMP}  periodo=${YEAR}-${MONTH}"
echo " log=${LOG}"
echo "============================================================"

oece_fail=0
prod6_fail=0
prod2_fail=0

echo
echo ">>> 1/3 OECE (sin proxy residencial)"
(
  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
  "$PY" "$ROOT/pipeline_seace.py" \
    --year "$YEAR" --month "$MONTH" --min-days 0 --skip-seace
)
if [[ $? -ne 0 ]]; then
  oece_fail=1
  echo "[!] OECE falló — se aborta el embudo (no se gasta proxy/2Captcha)."
  echo "Fin pipeline ${STAMP} oece=${oece_fail}" >> "$LOG"
  exit 1
fi

echo
echo ">>> 2/3 PROD6 radar ${HORAS}h (IPRoyal, sin 2Captcha)"
"$PY" "$ROOT/scraper_prod6.py" --anio "$YEAR" --horas-radar "$HORAS"
if [[ $? -ne 0 ]]; then
  prod6_fail=1
  echo "[!] PROD6 falló — se registra y se continúa con PROD2."
fi

echo
echo ">>> 3/3 PROD2 radar ${HORAS}h (IPRoyal + 2Captcha, solo nuevas no bloqueadas)"
"$PY" "$ROOT/scraper_prod2.py" --year "$YEAR" --objeto "" --horas-radar "$HORAS"
if [[ $? -ne 0 ]]; then
  prod2_fail=1
  echo "[!] PROD2 falló."
fi

echo
echo "Resumen: oece_fail=${oece_fail} prod6_fail=${prod6_fail} prod2_fail=${prod2_fail}"
echo "Fin pipeline ${STAMP}"

if [[ $prod2_fail -ne 0 ]]; then
  exit 1
fi
exit 0
