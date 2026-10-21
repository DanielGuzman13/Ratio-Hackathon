"""Usuarios: registro con correo único, inicio de sesión, administrador inicial e historial."""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone

import httpx

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..auth import hash_password, make_token, verify_password
from ..config import settings
from ..models import AIBatch, Area, DiagnosticSession, Question, Report, Response, User
from .diagnostic import DiagnosticError, download_allowed

log = logging.getLogger("ratio.users")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def me_payload(db: Session, user: User) -> dict:
    from .diagnostic import account_formation
    return user_payload(user) | {"formacion": account_formation(db, user)}


def user_payload(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role, "provider": user.provider or "correo"}


def register(db: Session, name: str, email: str, password: str) -> dict:
    email = normalize_email(email)
    if db.scalar(select(User).where(User.email == email)):
        raise DiagnosticError("Ya existe una cuenta con ese correo. Inicia sesión.", 409)
    user = User(email=email, name=name.strip(), password_hash=hash_password(password), role="usuario",
                last_login=datetime.now(timezone.utc))
    db.add(user)
    db.commit()
    return {"token": make_token(user.id, user.role, user.token_version or 0), "user": user_payload(user)}


def login(db: Session, email: str, password: str) -> dict:
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if not user or not verify_password(password, user.password_hash):
        raise DiagnosticError("Correo o contraseña incorrectos.", 401)
    if settings.production and user.role == "admin" and password == DEFAULT_ADMIN_PASSWORD:
        raise DiagnosticError("Por seguridad, la contraseña por defecto del administrador no funciona en producción. "
                              "Define ADMIN_PASSWORD en el servidor y reinícialo.", 403)
    user.last_login = datetime.now(timezone.utc)
    db.commit()
    return {"token": make_token(user.id, user.role, user.token_version or 0), "user": user_payload(user)}


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
    return {"token": make_token(user.id, user.role, user.token_version or 0), "user": user_payload(user)}


DEFAULT_ADMIN_PASSWORD = "admin-demo"


def ensure_admin(db: Session) -> None:
    """Crea el administrador inicial (ADMIN_EMAIL / ADMIN_PASSWORD) si no existe.

    Si ya existe con la contraseña de demostración y ADMIN_PASSWORD trae otra, la reemplaza
    (así basta con definir la variable en el servidor para cerrar ese riesgo).
    """
    email = normalize_email(settings.admin_email)
    if not email:
        return
    existing = db.scalar(select(User).where(User.email == email))
    if existing and existing.role != "admin":
        # La cuenta ya existía (p. ej. entró con Google): se promueve a administrador.
        existing.role = "admin"
        if settings.admin_password and settings.admin_password != DEFAULT_ADMIN_PASSWORD:
            existing.password_hash = hash_password(settings.admin_password)  # también puede entrar con contraseña
        existing.token_version = (existing.token_version or 0) + 1  # su sesión abierta se renueva con el nuevo rol
        db.commit()
        log.warning("La cuenta %s se promovió a administrador (ADMIN_EMAIL).", email)
        return
    if existing:
        if (settings.admin_password and settings.admin_password != DEFAULT_ADMIN_PASSWORD
                and verify_password(DEFAULT_ADMIN_PASSWORD, existing.password_hash)):
            existing.password_hash = hash_password(settings.admin_password)
            existing.token_version = (existing.token_version or 0) + 1
            db.commit()
            log.warning("Se reemplazó la contraseña por defecto del administrador %s.", email)
        elif settings.production and verify_password(DEFAULT_ADMIN_PASSWORD, existing.password_hash):
            log.warning("El administrador %s usa la contraseña por defecto: define ADMIN_PASSWORD.", email)
        return
    db.add(User(email=email, name=settings.admin_name, password_hash=hash_password(settings.admin_password),
                role="admin"))
    db.commit()


def change_password(db: Session, user: User, current: str, new: str) -> dict:
    if user.provider != "google" or current:
        if not verify_password(current, user.password_hash):
            raise DiagnosticError("La contraseña actual no es correcta.", 401)
    if new == DEFAULT_ADMIN_PASSWORD:
        raise DiagnosticError("Elige otra contraseña.", 422)
    user.password_hash = hash_password(new)
    user.token_version = (user.token_version or 0) + 1  # cierra las demás sesiones
    db.commit()
    return {"token": make_token(user.id, user.role, user.token_version), "user": user_payload(user)}


def export_data(db: Session, user: User) -> dict:
    """Ley 1581 (acceso): todo lo que Ratio guarda de la persona, en un JSON descargable."""
    sessions = db.scalars(select(DiagnosticSession).where(DiagnosticSession.user_id == user.id)
                          .order_by(DiagnosticSession.created_at)).all()
    out = []
    for s in sessions:
        out.append({
            "id": s.id, "creado": s.created_at.isoformat() if s.created_at else None, "estado": s.status,
            "perfil": s.profile,
            "respuestas": [{"n": r.seq, "pregunta": r.question.stem, "respuesta": r.answer, "acierto": r.is_correct}
                           for r in s.responses],
            "resultado": s.report.result.get("global") if s.report else None,
            "ruta": [{"paso": i.get("step"), "tema": i.get("subtopic"), "objetivo": i.get("objective")}
                     for i in (s.report.route if s.report else [])],
        })
    return {"cuenta": {"nombre": user.name, "correo": user.email, "proveedor": user.provider,
                       "creada": user.created_at.isoformat() if user.created_at else None},
            "diagnosticos": out,
            "exportado": datetime.now(timezone.utc).isoformat()}


def delete_account(db: Session, user: User, password: str) -> dict:
    """Ley 1581 (supresión): borra la cuenta, sus diagnósticos, respuestas e informes.

    Las preguntas que la IA generó para la persona se conservan en el banco pero se desvinculan de ella.
    """
    if user.role == "admin":
        raise DiagnosticError("La cuenta de administrador no se puede eliminar desde aquí.", 403)
    if user.provider != "google" and not verify_password(password, user.password_hash):
        raise DiagnosticError("La contraseña no es correcta.", 401)
    session_ids = list(db.scalars(select(DiagnosticSession.id).where(DiagnosticSession.user_id == user.id)))
    if session_ids:
        db.execute(update(Question).where(Question.session_id.in_(session_ids)).values(session_id=None))
        db.execute(delete(Response).where(Response.session_id.in_(session_ids)))
        db.execute(delete(Report).where(Report.session_id.in_(session_ids)))
        db.execute(delete(AIBatch).where(AIBatch.session_id.in_(session_ids)))
        db.execute(delete(DiagnosticSession).where(DiagnosticSession.id.in_(session_ids)))
    db.delete(user)
    db.commit()
    return {"deleted": True, "diagnosticos": len(session_ids)}


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
