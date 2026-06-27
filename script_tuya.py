from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ---------- Configuración ----------

TUYA_ENDPOINTS = {
    "eu": "https://openapi.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}

VOLTAGE_CODES = ("cur_voltage", "voltage", "voltage_a", "phase_a")
VOLTAGE_SCALE = 10.0


def env(name: str, required: bool = True, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"[ERROR] Falta la variable de entorno: {name}", file=sys.stderr)
        sys.exit(1)
    return val or ""


# ---------- Firma Tuya v2 ----------

def _sign(client_id: str, secret: str, t: str, access_token: str, method: str,
          path: str, body: str = "") -> str:
    content_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    string_to_sign = f"{method}\n{content_sha256}\n\n{path}"
    payload = f"{client_id}{access_token}{t}{string_to_sign}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"),
                    hashlib.sha256).hexdigest().upper()


def _sign_token(client_id: str, secret: str, t: str, method: str, path: str) -> str:
    content_sha256 = hashlib.sha256(b"").hexdigest()
    string_to_sign = f"{method}\n{content_sha256}\n\n{path}"
    payload = f"{client_id}{t}{string_to_sign}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"),
                    hashlib.sha256).hexdigest().upper()


def get_access_token(base_url: str, client_id: str, secret: str) -> str:
    path = "/v1.0/token?grant_type=1"
    t = str(int(time.time() * 1000))
    sign = _sign_token(client_id, secret, t, "GET", path)
    headers = {
        "client_id": client_id,
        "sign": sign,
        "sign_method": "HMAC-SHA256",
        "t": t,
    }
    r = requests.get(base_url + path, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Tuya token error: {data}")
    return data["result"]["access_token"]


def get_device_status(base_url: str, client_id: str, secret: str,
                      access_token: str, device_id: str) -> list[dict]:
    path = f"/v1.0/iot-03/devices/{device_id}/status"
    t = str(int(time.time() * 1000))
    sign = _sign(client_id, secret, t, access_token, "GET", path)
    headers = {
        "client_id": client_id,
        "access_token": access_token,
        "sign": sign,
        "sign_method": "HMAC-SHA256",
        "t": t,
    }
    r = requests.get(base_url + path, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Tuya status error: {data}")
    return data["result"]


def extract_voltage(status: list[dict]) -> float:
    by_code = {item["code"]: item["value"] for item in status}
    for code in VOLTAGE_CODES:
        if code in by_code:
            raw = by_code[code]
            try:
                value = float(raw) / VOLTAGE_SCALE
            except (TypeError, ValueError):
                continue
            if 50 <= value <= 500:
                return round(value, 2)
    raise RuntimeError(
        f"No se encontró voltaje en el status del dispositivo. Códigos: {list(by_code.keys())}"
    )


def insert_voltage(supabase_url: str, publishable_key: str, voltaje: float) -> None:
    url = f"{supabase_url.rstrip('/')}/rest/v1/lecturas_voltaje"
    headers = {
        "apikey": publishable_key,
        "Authorization": f"Bearer {publishable_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "voltaje": voltaje,
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase insert error {r.status_code}: {r.text}")


# ---------- Main con Sistema de Reintentos Automáticos ----------

def main() -> int:
    supabase_url = env("SUPABASE_URL")
    publishable_key = env("SUPABASE_PUBLISHABLE_KEY")
    client_id = env("TUYA_CLIENT_ID")
    client_secret = env("TUYA_CLIENT_SECRET")
    device_id = env("TUYA_DEVICE_ID")
    region = env("TUYA_REGION", required=False, default="eu").lower()

    base_url = TUYA_ENDPOINTS.get(region)
    if not base_url:
        print(f"[ERROR] Región Tuya desconocida: {region}", file=sys.stderr)
        return 1

    intentos_maximos = 4
    tiempo_espera = 15  # segundos entre intentos

    for intento in range(1, intentos_maximos + 1):
        try:
            print(f"[INFO] Intento {intento}/{intentos_maximos}...")
            token = get_access_token(base_url, client_id, client_secret)
            status = get_device_status(base_url, client_id, client_secret, token, device_id)
            voltage = extract_voltage(status)
            insert_voltage(supabase_url, publishable_key, voltage)
            print(f"[OK] {datetime.now().isoformat(timespec='seconds')} -> voltaje={voltage} V")
            return 0  # Éxito total, salimos del script
        except Exception as e:
            print(f"[ADVERTENCIA] El intento {intento} falló debido a: {e}", file=sys.stderr)
            if intento < intentos_maximos:
                print(f"[INFO] Esperando {tiempo_espera} segundos antes del siguiente intento...", file=sys.stderr)
                time.sleep(tiempo_espera)
            else:
                print("[ERROR] Se agotaron todos los intentos de ejecución.", file=sys.stderr)
                return 1


if __name__ == "__main__":
    sys.exit(main())
