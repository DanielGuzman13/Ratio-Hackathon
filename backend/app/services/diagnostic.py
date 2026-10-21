"""Orquestación del diagnóstico (capa de servicio).

Une la base de datos con el motor puro (app.engine) y la IA opcional (app.ai).
Los routers HTTP solo llaman a estas funciones; así la lógica se puede reusar
desde un chatbot, un CLI o pruebas sin pasar por HTTP.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import flag_modified

from ..ai.client import AIUnavailable
from ..ai.service import AIRubricGrader, ai_status
from ..config import settings
from ..engine import adaptive
from ..engine.classification import classify_global, classify_subtopic
from ..engine.grading import KeyGrader, RubricKeywordGrader
from ..engine.profiling import GOAL_LABELS, PROFILE_LABELS, build_plan
from ..engine.routing import build_route
from ..models import Area, DiagnosticSession, Question, Report, Response, Subtopic
from . import mailer, live_ai, preparation

LIVE_STATUSES = ("aprobada", "verificada")


class DiagnosticError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _area(db: Session, area_id: str) -> Area:
    area = db.scalar(select(Area).where(Area.id == area_id).options(
        selectinload(Area.subtopics).selectinload(Subtopic.sources)))
    if not area:
        raise DiagnosticError(f"Área desconocida: {area_id}", 404)
    return area


def _fit(q: Question, session: DiagnosticSession | None) -> int:
    """Qué tan a la medida es una pregunta para esta persona (menor = mejor)."""
    if session is not None and q.session_id == session.id:
        return 0  # la IA la generó para esta persona, con su perfil y sus errores
    perfil = session.profile.get("perfil") if session is not None else None
    if q.origin == "ia" and perfil and q.profile_tag == perfil:
        return 1  # generada para otra persona con el mismo perfil
    if q.origin != "ia" or not q.profile_tag:
        return 2  # curada por el equipo, o precargada por IA sin un perfil específico
    return 3      # generada para un perfil distinto


def _bank(db: Session, area_id: str, session: DiagnosticSession | None = None) -> list[adaptive.QuestionRef]:
    query = select(Question).join(Subtopic).where(Subtopic.area_id == area_id, Question.status.in_(LIVE_STATUSES))
    if session is not None and session.state:
        query = query.where(Question.subtopic_id.in_(session.state["order"]))  # solo los temas elegidos
    rows = db.scalars(query).all()
    return [adaptive.QuestionRef(q.id, q.subtopic_id, q.level, q.approach, q.kind, q.origin,
                                 _fit(q, session), q.times_shown or 0) for q in rows]


def _session(db: Session, session_id: str, user=None) -> DiagnosticSession:
    """Carga la sesión y verifica que pertenezca al usuario (o que sea un administrador)."""
    session = db.get(DiagnosticSession, session_id)
    if not session:
        raise DiagnosticError("Sesión no encontrada", 404)
    if user is not None and user.role != "admin" and session.user_id != user.id:
        raise DiagnosticError("Este diagnóstico pertenece a otro usuario", 403)
    return session


def download_allowed(report: Report | None) -> bool:
    """El informe se descarga si no tenía marcas dudosas o si un administrador ya lo aprobó."""
    return bool(report) and report.review_status in ("no_requiere", "aprobada", "ajustada")


def list_areas(db: Session) -> list[dict]:
    areas = db.scalars(select(Area).where(Area.origin == "curada").options(selectinload(Area.subtopics))).all()
    return [{"id": a.id, "name": a.name, "description": a.description,
             "subtopics": [{"id": s.id, "name": s.name, "module": s.module, "concepts": s.concepts}
                           for s in a.subtopics]} for a in areas]


def build_initial_state(db: Session, area_id: str, profile: dict) -> dict:
    """Estado inicial del motor. Si la persona eligió temas, se evalúan ÚNICAMENTE esos."""
    area = _area(db, area_id)
    plan = build_plan(profile)
    chosen = set(profile.get("intereses") or [])
    subtopics = [s for s in area.subtopics if not chosen or s.id in chosen]
    # Los prerrequisitos solo se exploran dentro de los temas elegidos.
    ids = {s.id for s in subtopics}
    refs = [adaptive.SubtopicRef(s.id, s.name, tuple(p for p in s.prerequisites if p in ids), s.order)
            for s in subtopics]
    budget, per = question_budget(len(refs))
    return adaptive.new_state(refs, plan.as_dict(), [], budget, per,
                              prefer_generated=settings.ai_prefer_generated)


def question_budget(n_topics: int) -> tuple[int, int]:
    """(total de preguntas, preguntas por tema). 1 tema = 15; cada tema adicional suma 5 (máximo 45)."""
    n = max(1, n_topics)
    total = min(45, 15 + 5 * (n - 1))
    return total, math.ceil(total / n)


def _free_topic_area(db: Session, topic: str) -> tuple[Area, bool]:
    """Devuelve (área, ya_existía). El área de un tema libre se reutiliza como caché."""
    area_id = "ia-" + preparation.slugify(topic)
    area = db.get(Area, area_id)
    if area and area.subtopics:
        return area, True
    if area is None:
        area = Area(id=area_id, name=topic[:120], description="Preparando con IA…", origin="ia", topic_query=topic)
        db.add(area)
        db.commit()
    return area, False


def start_session(db: Session, profile: dict, user=None) -> dict:
    profile.pop("consentimiento", None)
    if user is not None:
        profile["nombre"], profile["email"] = user.name, user.email
    topic = (profile.get("tema_libre") or "").strip()
    job = None
    if profile["area_id"] == "libre":
        if not topic:
            raise DiagnosticError("Escribe el tema que quieres valorar.", 422)
        if not (settings.ai_free_topic and preparation.ai_live_enabled()):
            raise DiagnosticError("El tema libre requiere IA configurada (AI_BASE_URL, AI_API_KEY y modelo).", 503)
        area, cached = _free_topic_area(db, topic)
        profile["area_id"] = area.id
        profile["intereses"] = []
        if not cached:
            job = preparation.create_job(db, kind="tema", area=area, topic=topic)
    else:
        area = _area(db, profile["area_id"])
        valid_ids = {s.id for s in area.subtopics}
        profile["intereses"] = [i for i in profile.get("intereses", []) if i in valid_ids]
        # Las áreas de la lista arrancan al instante con el banco; la IA trabaja en paralelo (live_ai).
    plan = build_plan(profile)
    # En un tema libre nuevo los subtemas aún no existen: el estado se arma cuando la IA termina el mapa.
    state = build_initial_state(db, area.id, profile) if area.subtopics else {}
    session = DiagnosticSession(area_id=area.id, profile=profile, approach=plan.approach, state=state,
                                job_id=job.id if job else None, user_id=user.id if user is not None else None)
    db.add(session)
    db.commit()
    if job:
        preparation.launch(job.id, session.id)
    elif state:
        live_ai.maybe_trigger(db, session)  # primer lote: preguntas a la medida del perfil
    return {"session_id": session.id, "area": area.name, "plan": plan.as_dict(),
            "preparing": job is not None,
            "progress": adaptive.progress(state) if state else None}


def preparation_status(db: Session, session_id: str, user=None) -> dict:
    session = _session(db, session_id, user)
    db.refresh(session)
    job = preparation.job_payload(db, session.job_id)
    pending = preparation.pending_for(db, session.job_id)
    has_state = bool(session.state)
    ready = has_state and (job is None or job["status"] != "en_curso"
                           or any(sid not in pending for sid in session.state.get("order", [])))
    area = db.get(Area, session.area_id)
    return {"session_id": session.id, "area": area.name if area else "", "job": job,
            "ready_to_start": bool(ready and (job is None or job["status"] != "error" or has_state)),
            "failed": bool(job and job["status"] == "error" and not has_state)}


LETTERS = "ABCDEFGH"


def shuffled_options(q: Question, seed: int) -> list[tuple[str, dict]]:
    """Opciones en orden aleatorio por sesión, reetiquetadas A, B, C… -> [(letra_mostrada, opción_original)].

    Evita que la respuesta correcta caiga siempre en la misma letra. Es estable dentro de la
    sesión (recargar la página no cambia el orden) y distinta entre sesiones.
    """
    ordered = sorted(q.options, key=lambda o: adaptive.jitter(seed, f"{q.id}:{o['key']}"))
    return [(LETTERS[i], o) for i, o in enumerate(ordered)]


def _public_question(q: Question, progress: dict, seed: int) -> dict:
    """Lo que ve el usuario: sin clave, sin errores asociados, sin fuente (no hay retroalimentación)."""
    return {
        "id": q.id, "kind": q.kind, "stem": q.stem,
        "options": [{"key": shown, "text": o["text"]} for shown, o in shuffled_options(q, seed)] if q.kind == "mc" else [],
        "level": q.level, "subtopic": q.subtopic.name, "progress": progress,
    }


def next_question(db: Session, session_id: str, user=None) -> dict:
    session = _session(db, session_id, user)
    if session.status != "en_curso":
        return {"finished": True}
    if not session.state:
        return {"finished": False, "waiting": True, "message": "La IA está preparando el tema…"}
    state = copy.deepcopy(session.state)
    was_current = (state.get("current") or {}).get("question_id")
    bank = _bank(db, session.area_id, session)
    ref = adaptive.next_question(state, bank, preparation.pending_for(db, session.job_id),
                                 live_ai.refillable(db, session))
    session.state = state
    flag_modified(session, "state")
    if isinstance(ref, adaptive.QuestionRef) and ref.id != was_current:
        db.get(Question, ref.id).times_shown = (db.get(Question, ref.id).times_shown or 0) + 1
    db.commit()
    if ref == adaptive.WAIT:
        if state.get("needs_refill"):
            live_ai.maybe_trigger(db, session, urgent=state["needs_refill"])
            return {"finished": False, "waiting": True, "progress": adaptive.progress(state),
                    "ai": live_ai.session_ai_status(db, session.id),
                    "message": "La IA está generando más preguntas de este tema según tus respuestas…"}
        return {"finished": False, "waiting": True, "progress": adaptive.progress(state),
                "ai": live_ai.session_ai_status(db, session.id),
                "message": "La IA está terminando de generar y verificar las preguntas del siguiente subtema…"}
    if ref is None:
        return {"finished": True, "progress": adaptive.progress(state)}
    question = db.get(Question, ref.id)
    public = _public_question(question, adaptive.progress(state), state.get("seed", 0))
    public["tailored"] = question.session_id == session.id  # generada por IA para esta persona
    return {"finished": False, "question": public, "ai": live_ai.session_ai_status(db, session.id)}


def _grader_for(question: Question):
    if question.kind == "mc":
        return KeyGrader()
    if ai_status()["verifier"]:
        return AIRubricGrader()
    return RubricKeywordGrader()


def submit_answer(db: Session, session_id: str, question_id: str, answer: str, user=None) -> dict:
    session = _session(db, session_id, user)
    # Idempotente: si la respuesta ya llegó (p. ej. se reenvió tras perder la conexión), no se duplica.
    already = db.scalar(select(Response).where(Response.session_id == session.id,
                                               Response.question_id == question_id))
    if already:
        return {"recorded": True, "duplicate": True, "progress": adaptive.progress(session.state)}
    if session.status != "en_curso":
        raise DiagnosticError("La sesión ya terminó", 409)
    state = copy.deepcopy(session.state)
    current = state.get("current")
    if not current or current["question_id"] != question_id:
        raise DiagnosticError("La respuesta no corresponde a la pregunta en curso", 409)
    question = db.get(Question, question_id)
    if question.kind == "mc":
        # La persona responde con la letra que vio; se traduce a la clave original de la opción.
        mapping = {shown: o["key"] for shown, o in shuffled_options(question, state.get("seed", 0))}
        answer = mapping.get(answer.strip().upper(), answer)
    grader = _grader_for(question)
    try:
        result = grader.grade(question, answer)
    except AIUnavailable:
        result = RubricKeywordGrader().grade(question, answer)
    ref = adaptive.QuestionRef(question.id, question.subtopic_id, question.level, question.approach, question.kind,
                               question.origin)
    decision = adaptive.register_answer(state, ref, result.score, result.correct, result.misconception)
    db.add(Response(session_id=session.id, question_id=question.id, seq=state["count"], answer=answer,
                    score=result.score, is_correct=result.correct, misconception=result.misconception,
                    rubric_hits=result.rubric_hits, grader=result.grader))
    if result.correct:
        question.times_correct = (question.times_correct or 0) + 1  # calibración empírica de dificultad
    session.state = state
    flag_modified(session, "state")
    db.commit()
    live_ai.maybe_trigger(db, session)  # cada pocas respuestas, la IA prepara más preguntas según lo observado
    # Sin retroalimentación al usuario: el diagnóstico evalúa, no enseña. La decisión
    # del motor queda en la traza, visible solo en el informe y para el docente.
    del decision
    return {"recorded": True, "progress": adaptive.progress(state)}


def finish_session(db: Session, session_id: str, user=None) -> dict:
    session = _session(db, session_id, user)
    if session.report:
        return report_payload(session)
    area = _area(db, session.area_id)
    state = session.state
    plan = state["plan"]
    subtopics = {s.id: {"name": s.name, "objective_tecnico": s.objective_tecnico,
                        "objective_practico": s.objective_practico, "concepts": s.concepts,
                        "risk_level": s.risk_level, "risk_note": s.risk_note} for s in area.subtopics}
    sources = {s.id: [{"id": x.id, "kind": x.kind, "citation": x.citation, "detail": x.detail, "url": x.url,
                       "min_level": x.min_level, "priority": x.priority, "verified_on": x.verified_on,
                       "risk_level": x.risk_level, "risk_note": x.risk_note} for x in s.sources]
               for s in area.subtopics}
    evidence: dict[str, list[dict]] = {sid: [] for sid in subtopics}
    for r in session.responses:
        q = r.question
        evidence[q.subtopic_id].append({
            "question_id": q.id, "level": q.level, "score": r.score, "correct": r.is_correct,
            "misconception": r.misconception, "concept": q.concept, "risk_level": q.risk_level,
            "risk_note": q.risk_note, "origin": q.origin, "grader": r.grader, "stem": q.stem,
            "tailored": q.session_id == session.id})
    per_subtopic = {sid: classify_subtopic(ev) | {"name": subtopics[sid]["name"]} for sid, ev in evidence.items()}
    global_result = classify_global(per_subtopic, plan["approach"], plan["max_level"])
    profile = session.profile
    probed_by = {sid: s.get("probed_by") for sid, s in state["subtopics"].items()}
    route = build_route(per_subtopic=per_subtopic, subtopics=subtopics, sources=sources, evidence=evidence,
                        prerequisites=state["prerequisites"], approach=plan["approach"],
                        interests=profile.get("intereses", []), hours_per_week=profile["horas_semana"],
                        weeks=profile["semanas"], probed_by=probed_by, order=state["order"])
    result = {
        "global": global_result,
        "subtopics": per_subtopic,
        "plan": plan,
        "trace": state["trace"],
        "strengths": [r["name"] for r in per_subtopic.values() if r["status"] == "fortaleza"],
        "gaps": [r["name"] for r in per_subtopic.values() if r["status"] == "vacio"],
        "developing": [r["name"] for r in per_subtopic.values() if r["status"] == "en_desarrollo"],
        "review_required": any(item["requires_review"] for item in route),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    session.status = "finalizada"
    session.finished_at = datetime.now(timezone.utc)
    db.add(Report(session_id=session.id, result=result, route=route,
                  review_status="pendiente" if result["review_required"] else "no_requiere"))
    db.commit()
    db.refresh(session)
    if not result["review_required"]:
        mailer.send_report_async(session.id)  # sin nada que verificar: el informe sale de una vez
    return report_payload(session)


def report_payload(session: DiagnosticSession) -> dict:
    report = session.report
    profile = dict(session.profile)
    area = session.area
    return {
        "session_id": session.id,
        "area_id": session.area_id,
        "area_origin": area.origin if area else "curada",
        "area_name": area.name if area else "",
        "profile": profile | {"perfil_label": PROFILE_LABELS.get(profile.get("perfil"), ""),
                              "objetivo_label": GOAL_LABELS.get(profile.get("objetivo"), "")},
        "result": report.result,
        "route": report.route,
        "review": {"status": report.review_status, "notes": report.teacher_notes,
                   "reviewed_by": report.reviewed_by,
                   "reviewed_at": report.reviewed_at.isoformat() if report.reviewed_at else None},
        "download_allowed": download_allowed(report),
        "email": mailer.email_info(report) | {"to": profile.get("email", "")},
        "questions": [{
            "seq": r.seq, "subtopic": r.question.subtopic.name, "level": r.question.level, "stem": r.question.stem,
            "correct": r.is_correct, "origin": r.question.origin,
            "tailored": r.question.session_id == session.id} for r in session.responses],
        "created_at": session.created_at.isoformat() if session.created_at else None,
    }


def run_ai(db: Session, session_id: str, user=None) -> dict:
    _session(db, session_id, user)  # solo la dueña de la sesión (o un admin) puede pedirlo
    return live_ai.run_pending(db, session_id)


def resend_report(db: Session, session_id: str, user=None) -> dict:
    """Reenvía el informe verificado al correo de la persona (máximo uno cada 2 minutos)."""
    session = _session(db, session_id, user)
    report = session.report
    if not report or not download_allowed(report):
        raise DiagnosticError("El informe aún está en verificación; se enviará a tu correo cuando se apruebe.", 409)
    if not mailer.enabled():
        raise DiagnosticError("El envío por correo no está configurado en este servidor.", 503)
    if report.email_status == "enviando":
        raise DiagnosticError("El correo ya se está enviando.", 409)
    sent = report.email_sent_at
    if sent and user is not None and user.role != "admin":
        sent = sent if sent.tzinfo else sent.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - sent).total_seconds() < 120:
            raise DiagnosticError("Ya te lo enviamos hace un momento. Revisa también la carpeta de spam.", 429)
    mailer.send_report_async(session.id)
    return {"status": "enviando", "to": session.profile.get("email", "")}


def get_report(db: Session, session_id: str, user=None) -> dict:
    session = _session(db, session_id, user)
    if not session.report:
        raise DiagnosticError("El diagnóstico aún no ha terminado", 409)
    return report_payload(session)
