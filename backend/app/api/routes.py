"""Endpoints HTTP. Delgados: validan entrada, autentican, llaman al servicio y traducen errores."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from ..ai.service import ai_status
from ..auth import read_token
from ..config import settings
from ..db import get_db
from ..models import User
from ..schemas import AnswerIn, DecisionIn, DeleteAccountIn, GenerateIn, GoogleIn, LoginIn, PasswordIn, ProfileIn, RegisterIn, ReviewIn, SourceConfirmIn
from ..services import diagnostic, mailer, teacher, users
from ..security import enforce
from ..services.diagnostic import DiagnosticError

public = APIRouter(prefix="/api", tags=["diagnóstico"])
docente = APIRouter(prefix="/api/teacher", tags=["administrador"])


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DiagnosticError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def current_user(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> User:
    token = authorization.removeprefix("Bearer ").strip()
    data = read_token(token) if token else None
    user = db.get(User, data["sub"]) if data else None
    if user and (data.get("ver", 0) or 0) != (user.token_version or 0):
        user = None  # la contraseña cambió: las sesiones anteriores ya no sirven
    if not user:
        raise HTTPException(status_code=401, detail="Inicia sesión para continuar")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Solo un administrador puede hacer esto")
    return user


# --- Cuenta -----------------------------------------------------------------

@public.post("/auth/register", status_code=201)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    enforce(request, "registro", limit=5, window=3600)
    return _run(users.register, db, body.nombre, body.email, body.password)


@public.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    enforce(request, "login-ip", limit=20, window=900)
    enforce(request, "login-correo", limit=8, window=900, extra=body.email)
    return _run(users.login, db, body.email, body.password)


@public.post("/auth/google")
def google_login(body: GoogleIn, request: Request, db: Session = Depends(get_db)):
    enforce(request, "google", limit=20, window=900)
    return _run(users.google_login, db, body.credential)


@public.post("/auth/password")
def change_password(body: PasswordIn, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    enforce(request, "clave", limit=8, window=900, extra=user.id)
    return _run(users.change_password, db, user, body.current, body.new)


@public.get("/me/export")
def export_my_data(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return users.export_data(db, user)


@public.post("/me/delete")
def delete_my_account(body: DeleteAccountIn, request: Request, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    enforce(request, "eliminar", limit=5, window=900, extra=user.id)
    return _run(users.delete_account, db, user, body.password)


@public.get("/auth/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return users.me_payload(db, user)


@public.get("/me/sessions")
def my_history(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return users.history(db, user)


# --- Público ----------------------------------------------------------------

@public.get("/health")
def health():
    return {"status": "ok"}


@public.get("/meta")
def meta():
    return {"app_name": settings.app_name, "tagline": settings.app_tagline, "ai": ai_status(),
            "ai_questions_per_subtopic": settings.ai_questions_per_subtopic,
            "google_client_id": settings.google_client_id or None, "mail_enabled": mailer.enabled(),
            "contact_email": settings.contact_email or mailer.sender() or None}


@public.get("/areas")
def areas(db: Session = Depends(get_db)):
    return diagnostic.list_areas(db)


# --- Diagnóstico (requiere sesión iniciada) ----------------------------------

@public.post("/sessions", status_code=201)
def start(profile: ProfileIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _run(diagnostic.start_session, db, profile.model_dump(), user)


@public.get("/sessions/{session_id}/preparation")
def preparation(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _run(diagnostic.preparation_status, db, session_id, user)


@public.get("/sessions/{session_id}/next")
def next_question(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _run(diagnostic.next_question, db, session_id, user)


@public.post("/sessions/{session_id}/answers")
def answer(session_id: str, body: AnswerIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _run(diagnostic.submit_answer, db, session_id, body.question_id, body.answer, user)


@public.post("/sessions/{session_id}/finish")
def finish(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _run(diagnostic.finish_session, db, session_id, user)


@public.get("/sessions/{session_id}/report")
def report(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _run(diagnostic.get_report, db, session_id, user)


@public.post("/sessions/{session_id}/ai/run")
def run_ai(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Serverless: el navegador pide aquí que se generen las preguntas de IA pendientes."""
    return _run(diagnostic.run_ai, db, session_id, user)


@public.post("/sessions/{session_id}/email")
def email_report(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _run(diagnostic.resend_report, db, session_id, user)


# --- Administrador ------------------------------------------------------------

@docente.get("/reports")
def teacher_reports(status: str | None = None, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    return teacher.list_reports(db, status)


@docente.get("/reports/{session_id}")
def teacher_report(session_id: str, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    return _run(teacher.get_report, db, session_id)


@docente.post("/reports/{session_id}/review")
def teacher_review(session_id: str, body: ReviewIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    return _run(teacher.review_report, db, session_id, reviewer=admin.name,
                checklist={k: v.model_dump() for k, v in body.checklist.items()},
                edits={k: v.model_dump(exclude_unset=True) for k, v in body.edits.items()},
                notes=body.notes, approve=body.approve)


@docente.post("/reports/{session_id}/email")
def teacher_email(session_id: str, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    return _run(diagnostic.resend_report, db, session_id, admin)


@docente.get("/ai-usage")
def teacher_ai_usage(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Consumo de IA para vigilar la cuota: tokens desde el arranque, límites que informa el proveedor
    y lotes recientes guardados en la base de datos (sobreviven a reinicios)."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from ..ai.client import quota_blocked, usage_snapshot
    from ..models import AIBatch
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    batches = db.scalar(select(func.count()).select_from(AIBatch).where(AIBatch.created_at >= since)) or 0
    sessions = db.scalar(select(func.count(func.distinct(AIBatch.session_id))).where(AIBatch.created_at >= since)) or 0
    return usage_snapshot() | {"batches_24h": batches, "sessions_with_ai_24h": sessions,
                               "quota_blocked": quota_blocked(), "model": settings.generator.model}


@docente.get("/questions")
def teacher_questions(status: str | None = None, area_id: str | None = None, _: User = Depends(admin_user),
                      db: Session = Depends(get_db)):
    return teacher.list_questions(db, status, area_id)


@docente.post("/questions/generate", status_code=201)
def teacher_generate(body: GenerateIn, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    return _run(teacher.generate_question, db, body.subtopic_id, body.level, body.approach, body.source_id)


@docente.post("/questions/{question_id}/decision")
def teacher_decision(question_id: str, body: DecisionIn, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    return _run(teacher.decide_question, db, question_id, body.decision, body.note)


@docente.get("/risks")
def teacher_risks(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    return teacher.risk_inventory(db)


@docente.post("/sources/{source_id}/confirm")
def teacher_confirm_source(source_id: str, body: SourceConfirmIn, admin: User = Depends(admin_user),
                           db: Session = Depends(get_db)):
    return _run(teacher.confirm_source, db, source_id, admin.name or admin.email, body.confirm)
