"""Resolución de reCAPTCHA v2 vía 2Captcha (in.php / res.php)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests


def _load_dotenv():
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

IN_URL = "https://2captcha.com/in.php"
RES_URL = "https://2captcha.com/res.php"
POLL_SEC = 5
MAX_WAIT_SEC = 180


class CaptchaError(RuntimeError):
    pass


class CaptchaZeroBalance(CaptchaError):
    pass


def api_key():
    key = (
        os.environ.get("TWOCAPTCHA_API_KEY")
        or os.environ.get("CAPTCHA_API_KEY")
        or os.environ.get("APIKEY_2CAPTCHA")
        or ""
    ).strip()
    if not key:
        raise CaptchaError(
            "Falta TWOCAPTCHA_API_KEY (o CAPTCHA_API_KEY) en el entorno."
        )
    return key


def extraer_sitekey(html):
    """SEACE usa reCAPTCHA v3: hidden claveRecaptchav3SitioWeb o api.js?render=6L..."""
    if not html:
        return None
    import re
    patterns = [
        r'id="claveRecaptchav3SitioWeb"[^>]*value="([^"]+)"',
        r'name="claveRecaptchav3SitioWeb"[^>]*value="([^"]+)"',
        r'recaptcha/api\.js\?render=([0-9A-Za-z_-]+)',
        r'data-sitekey=["\']([^"\']+)["\']',
        r'sitekey["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'grecaptcha\.execute\(\s*["\']([^"\']+)["\']',
        r'(6L[0-9A-Za-z_-]{20,})',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return None


def extraer_action_v3(html, default="frmBuscadorProcedimientosSeleccion"):
    if not html:
        return default
    import re
    m = re.search(
        r"cargaTokenBuscadorProSel[\s\S]{0,400}?action:\s*['\"]([^'\"]+)['\"]",
        html,
        re.I,
    )
    return m.group(1) if m else default


def resolver_recaptcha(sitekey, page_url, key=None, version="v3", action=None, min_score=0.3):
    key = key or api_key()
    action = action or os.environ.get("SEACE_RECAPTCHA_ACTION") or "frmBuscadorProcedimientosSeleccion"
    version = (version or os.environ.get("SEACE_RECAPTCHA_VERSION") or "v3").lower()
    print(f"[*] 2Captcha: {version} sitekey={sitekey[:12]}... action={action}")
    data = {
        "key": key,
        "method": "userrecaptcha",
        "googlekey": sitekey,
        "pageurl": page_url,
        "json": 1,
    }
    if version == "v3":
        data["version"] = "v3"
        data["action"] = action
        data["min_score"] = str(min_score)
    r = requests.post(
        IN_URL,
        data=data,
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    status = str(payload.get("status"))
    request_id = str(payload.get("request") or "")

    if request_id == "ERROR_ZERO_BALANCE":
        raise CaptchaZeroBalance("2Captcha: ERROR_ZERO_BALANCE (saldo insuficiente).")
    if status != "1":
        raise CaptchaError(f"2Captcha in.php: {payload}")

    print(f"[*] 2Captcha: captcha id={request_id} — polling cada {POLL_SEC}s")
    deadline = time.time() + MAX_WAIT_SEC
    while time.time() < deadline:
        time.sleep(POLL_SEC)
        rr = requests.get(
            RES_URL,
            params={"key": key, "action": "get", "id": request_id, "json": 1},
            timeout=30,
        )
        rr.raise_for_status()
        data = rr.json()
        req = str(data.get("request") or "")
        if str(data.get("status")) == "1" and req:
            print(f"[+] 2Captcha resuelto (len={len(req)})")
            return req
        if req == "CAPCHA_NOT_READY":
            print("    ... aún no listo")
            continue
        if req == "ERROR_ZERO_BALANCE":
            raise CaptchaZeroBalance("2Captcha: ERROR_ZERO_BALANCE (saldo insuficiente).")
        raise CaptchaError(f"2Captcha res.php: {data}")

    raise CaptchaError(f"2Captcha timeout ({MAX_WAIT_SEC}s) para id={request_id}")
