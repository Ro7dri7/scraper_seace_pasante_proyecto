#!/usr/bin/env bash
# Sincronización diaria OECE (gratis, sin proxy ni 2Captcha).
set -u
cd "$(dirname "$0")"
ROOT="$(pwd)"

if [[ -f "$ROOT/venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/venv/bin/activate"
else
  echo "[-] No existe venv/bin/activate." >&2
  exit 1
fi

mkdir -p "$ROOT/logs"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
LOG="$ROOT/logs/oece_${STAMP}.log"
YEAR="$(date +%Y)"
MONTH="$(date +%m)"
PY="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"
PY="${PY:-python3}"

exec > >(tee -a "$LOG") 2>&1
echo "============================================================"
echo " Licity Go OECE diario  ${STAMP}  periodo=${YEAR}-${MONTH}"
echo "============================================================"

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
"$PY" "$ROOT/pipeline_seace.py" \
  --year "$YEAR" --month "$MONTH" --min-days 0 --skip-seace
exit $?
