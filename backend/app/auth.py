"""Autenticación sin dependencias externas.

* Contraseñas: PBKDF2-HMAC-SHA256 con sal aleatoria (biblioteca estándar).
* Sesión: token firmado con HMAC-SHA256 (payload + firma), con vencimiento.
* La clave de firma sale de SECRET_KEY o, si no existe, de un archivo .secret_key
  que se crea una vez en la carpeta del proyecto (así los tokens sobreviven reinicios).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from .config import BASE_DIR, settings

_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_b64, digest_b64 = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iterations))
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


def _secret() -> bytes:
    if settings.secret_key:
        return settings.secret_key.encode()
    path = Path(BASE_DIR) / ".secret_key"
    if not path.exists():
        try:
            path.write_text(secrets.token_hex(32), encoding="utf-8")
        except OSError as exc:  # p. ej. Vercel: el disco es de solo lectura
            raise RuntimeError("Define la variable SECRET_KEY (una cadena larga y aleatoria) en el servidor.") from exc
    return path.read_text(encoding="utf-8").strip().encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(user_id: str, role: str, version: int = 0) -> str:
    """`ver` permite invalidar todas las sesiones de un usuario (cambio de contraseña, cierre global)."""
    payload = _b64(json.dumps({"sub": user_id, "role": role, "ver": version,
                               "exp": int(time.time()) + settings.token_days * 86400}).encode())
    signature = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def read_token(token: str) -> dict | None:
    try:
        payload, signature = token.split(".")
    except ValueError:
        return None
    expected = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < time.time():
        return None
    return data
