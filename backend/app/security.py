"""Protecciones transversales: cabeceras de seguridad y límite de intentos.

- Cabeceras: CSP, nosniff, anti-clickjacking, Referrer-Policy y HSTS (en https).
- Límite de intentos: ventana deslizante en memoria por IP (y por correo en el ingreso).
  Es suficiente para una instancia; con varias instancias se movería a Redis.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

# La app usa estilos en línea (atributo style) y carga Google Fonts y Google Identity Services.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://accounts.google.com/gsi/client",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://accounts.google.com/gsi/style",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https:",
    "connect-src 'self' https://accounts.google.com/gsi/",
    "frame-src https://accounts.google.com/gsi/",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("Content-Security-Policy", CSP)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    # El popup de Google necesita poder comunicarse con la ventana que lo abrió.
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
    if request.url.scheme == "https":
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path.startswith("/api/"):
        headers.setdefault("Cache-Control", "no-store")
    return response


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float) -> bool:
        """Registra un intento; devuelve False si ya se superó el límite en la ventana."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            if len(self._hits) > 50_000:  # evita crecer sin límite
                self._hits = defaultdict(deque, {k: v for k, v in self._hits.items() if v})
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    # Con --proxy-headers (Dockerfile), uvicorn ya pone aquí la IP real del visitante.
    return request.client.host if request.client else "desconocida"


def enforce(request: Request, bucket: str, limit: int, window: float, extra: str = "") -> None:
    key = f"{bucket}:{client_ip(request)}:{extra.lower()}"
    if not limiter.hit(key, limit, window):
        minutes = max(1, round(window / 60))
        raise HTTPException(status_code=429, detail=f"Demasiados intentos. Espera unos {minutes} minutos e intenta de nuevo.")
