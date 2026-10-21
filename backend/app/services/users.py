"""Usuarios: registro con correo único, inicio de sesión, administrador inicial e historial."""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone

import httpx

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import hash_password, make_token, verify_password
from ..config import settings
from ..models import Area, DiagnosticSession, User
from .diagnostic import DiagnosticError, download_allowed


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def user_payload(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role}


def register(db: Session, name: str, email: str, password: str) -> dict:
    email = normalize_email(email)
    if db.scalar(select(User).where(User.email == email)):
        raise DiagnosticError("Ya existe una cuenta con ese correo. Inicia sesión.", 409)
    user = User(email=email, name=name.strip(), password_hash=hash_password(password), role="usuario",
                last_login=datetime.now(timezone.utc))
    db.add(user)
    db.commit()
    return {"token": make_token(user.id, user.role), "user": user_payload(user)}


def login(db: Session, email: str, password: str) -> dict:
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if not user or not verify_password(password, user.password_hash):
        raise DiagnosticError("Correo o contraseña incorrectos.", 401)
    user.last_login = datetime.now(timezone.utc)
    db.commit()
    return {"token": make_token(user.id, user.role), "user": user_payload(user)}


GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


def verify_google_credential(credential: str) -> dict:
    """Valida el ID token de «Iniciar sesión con Google» con el servicio oficial de Google."""
    if not settings.google_client_id:
        raise DiagnosticError("El inicio de sesión con Google no está configurado.", 503)
    try:
        res = httpx.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": credential}, timeout=15)
    except httpx.HTTPError as exc:
        raise DiagnosticError("No se pudo contactar a Google. Intenta de nuevo.", 502) from exc
    data = res.json() if res.status_code == 200 else {}
    if (not data or data.get("aud") != settings.google_client_id or data.get("iss") not in GOOGLE_ISSUERS
            or str(data.get("email_verified")).lower() != "true" or int(data.get("exp", 0)) < time.time()):
        raise DiagnosticError("Google no confirmó tu identidad. Intenta de nuevo.", 401)
    return data


def google_login(db: Session, credential: str) -> dict:
    info = verify_google_credential(credential)
    email = normalize_email(info["email"])
    user = db.scalar(select(User).where(User.email == email))
    if not user:  # primera vez: se crea la cuenta con los datos de Google (sin contraseña utilizable)
        user = User(email=email, name=(info.get("name") or email.split("@")[0])[:120],
                    password_hash=hash_password(secrets.token_urlsafe(32)), role="usuario", provider="google")
        db.add(user)
    user.last_login = datetime.now(timezone.utc)
    db.commit()
    return {"token": make_token(user.id, user.role), "user": user_payload(user)}


def ensure_admin(db: Session) -> None:
    """Crea el administrador inicial (ADMIN_EMAIL / ADMIN_PASSWORD) si no existe."""
    email = normalize_email(settings.admin_email)
    if not email or db.scalar(select(User).where(User.email == email)):
        return
    db.add(User(email=email, name=settings.admin_name, password_hash=hash_password(settings.admin_password),
                role="admin"))
    db.commit()


def history(db: Session, user: User) -> list[dict]:
    sessions = db.scalars(select(DiagnosticSession).where(DiagnosticSession.user_id == user.id)
                          .order_by(DiagnosticSession.created_at.desc())).all()
    out = []
    for s in sessions:
        area = db.get(Area, s.area_id)
        names = (s.state or {}).get("names", {})
        report = s.report
        out.append({
            "session_id": s.id,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "area": area.name if area else s.area_id,
            "topics": [names.get(t, t) for t in (s.state or {}).get("order", [])],
            "status": s.status,
            "answered": (s.state or {}).get("count", 0),
            "expected": (s.state or {}).get("max_questions", 0),
            "level_name": report.result["global"]["level_name"] if report else None,
            "review_status": report.review_status if report else None,
            "download_allowed": download_allowed(report) if report else False,
        })
    return out
