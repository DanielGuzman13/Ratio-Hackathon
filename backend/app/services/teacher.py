"""Servicios del panel docente: revisión de rutas, banco de preguntas y riesgos."""
from __future__ import annotations

import re

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import service as ai
from . import mailer
from ..ai.client import AIUnavailable
from ..models import DiagnosticSession, Question, Report, Source, Subtopic
from . import live_ai
from .diagnostic import DiagnosticError, report_payload


def list_reports(db: Session, status: str | None = None) -> list[dict]:
    query = select(Report).order_by(Report.created_at.desc())
    if status:
        query = query.where(Report.review_status == status)
    out = []
    for report in db.scalars(query).all():
        session = report.session
        flags = sum(len(item["review_flags"]) for item in report.route)
        out.append({
            "session_id": session.id,
            "nombre": session.profile.get("nombre") or "Anónimo",
            "email": session.profile.get("email", ""),
            "checklist_total": len(checklist_items(report)),
            "checklist_done": sum(1 for v in (report.checklist or {}).values() if v.get("checked")),
            "perfil": session.profile.get("perfil"),
            "area_id": session.area_id,
            "level_name": report.result["global"]["level_name"],
            "approach": report.result["global"]["approach"],
            "items": len(report.route),
            "items_to_review": sum(1 for item in report.route if item["requires_review"]),
            "flags": flags,
            "review_status": report.review_status,
            "created_at": report.created_at.isoformat() if report.created_at else None,
        })
    return out


def get_report(db: Session, session_id: str) -> dict:
    session = db.get(DiagnosticSession, session_id)
    if not session or not session.report:
        raise DiagnosticError("Informe no encontrado", 404)
    payload = report_payload(session)
    payload["ai_batches"] = live_ai.batches_payload(db, session_id)
    payload["checklist_items"] = checklist_items(session.report)
    payload["checklist"] = session.report.checklist or {}
    payload["original_route"] = session.report.original_route or session.report.route
    payload["user"] = {"name": session.profile.get("nombre"), "email": session.profile.get("email")}
    payload["responses"] = [{
        "seq": r.seq, "question_id": r.question_id, "subtopic_id": r.question.subtopic_id,
        "level": r.question.level, "kind": r.question.kind, "stem": r.question.stem,
        "answer": r.answer, "correct_key": r.question.correct_key, "score": r.score,
        "correct": r.is_correct, "misconception": r.misconception, "grader": r.grader,
        "origin": r.question.origin, "tailored": r.question.session_id == session.id} for r in session.responses]
    return payload


def _qid(flag: dict) -> str | None:
    if flag.get("question_id"):
        return flag["question_id"]
    match = re.search(r"[0-9a-f]{32}", flag.get("text", ""))
    return match.group(0) if match else None


def checklist_items(report: Report) -> list[dict]:
    """Elementos que el administrador debe revisar: marcas de riesgo medio o alto de la ruta original."""
    route = report.original_route or report.route
    items = []
    for item in route:
        flags = item.get("review_flags", [])
        # Informes antiguos: una pregunta de IA traía dos marcas (riesgo + IA); basta con revisarla una vez.
        ai_ids = {_qid(f) for f in flags if f["type"] == "pregunta_generada_por_ia"}
        for i, flag in enumerate(flags):
            if flag["type"] == "pregunta_con_riesgo" and _qid(flag) in ai_ids:
                continue
            if flag["severity"] in ("media", "alta"):
                items.append({"key": f"{item['subtopic_id']}#{i}", "subtopic_id": item["subtopic_id"],
                              "subtopic": item["subtopic"], "type": flag["type"], "severity": flag["severity"],
                              "text": flag["text"], "question_id": _qid(flag)})
    return items


def _same(field: str, new, old) -> bool:
    """El formulario reenvía todos los campos: solo cuenta como ajuste lo que de verdad cambió."""
    if field == "sources":
        key = lambda items: [((x.get("citation") or "").strip(), (x.get("url") or "").strip()) for x in items or []]  # noqa: E731
        return key(new) == key(old)
    if isinstance(new, str) and isinstance(old, str):
        return new.strip() == old.strip()
    if field == "teacher_note":
        return not new and not old
    return new == old


EDITABLE_FIELDS = ("objective", "focus_concepts", "sources", "teacher_note", "action")


def review_report(db: Session, session_id: str, *, reviewer: str, checklist: dict[str, dict], edits: dict[str, dict],
                  notes: str, approve: bool) -> dict:
    """Guarda la revisión del administrador y, si aprueba, habilita la descarga del informe.

    - checklist: {clave: {checked: bool, comment: str}} para cada elemento dudoso.
    - edits: {subtopic_id: {objective, focus_concepts, sources, teacher_note, action, removed}}: lo editado
      es lo que verá la persona y lo que sale en el PDF (la ruta original queda guardada).
    """
    session = db.get(DiagnosticSession, session_id)
    if not session or not session.report:
        raise DiagnosticError("Informe no encontrado", 404)
    report = session.report
    if not report.original_route:
        report.original_route = [dict(item) for item in report.route]
    items = checklist_items(report)
    valid_keys = {i["key"] for i in items}
    clean = {k: {"checked": bool(v.get("checked")), "comment": str(v.get("comment", ""))[:1000]}
             for k, v in checklist.items() if k in valid_keys}
    if approve:
        missing = [i for i in items if not clean.get(i["key"], {}).get("checked")]
        if missing:
            raise DiagnosticError(f"Faltan {len(missing)} elementos de la lista de verificación por revisar.", 422)
    changed = False
    route = []
    for item in report.original_route:
        item = dict(item)
        edit = edits.get(item["subtopic_id"], {})
        if edit.get("removed"):
            changed = True
            continue
        for field in EDITABLE_FIELDS:
            if field in edit and edit[field] is not None and not _same(field, edit[field], item.get(field)):
                item[field] = edit[field]
                if field != "teacher_note" or edit[field]:
                    changed = True
        item["admin_comments"] = [{"text": i["text"], "comment": clean[i["key"]]["comment"]}
                                  for i in items if i["subtopic_id"] == item["subtopic_id"]
                                  and clean.get(i["key"], {}).get("comment")]
        item["reviewed"] = approve
        route.append(item)
    for step, item in enumerate(route, start=1):
        item["step"] = step
    report.route = route
    report.checklist = clean
    report.teacher_notes = notes
    report.reviewed_by = reviewer
    report.reviewed_at = datetime.now(timezone.utc)
    report.review_status = ("ajustada" if changed else "aprobada") if approve else "en_revision"
    db.commit()
    if approve:
        mailer.send_report_async(session_id)  # verificado: el informe sale al correo de la persona
    return get_report(db, session_id)


def _question_payload(q: Question) -> dict:
    return {"id": q.id, "subtopic_id": q.subtopic_id, "subtopic": q.subtopic.name, "level": q.level,
            "approach": q.approach, "kind": q.kind, "concept": q.concept, "stem": q.stem,
            "options": q.options, "correct_key": q.correct_key, "rubric": q.rubric,
            "source": q.source.citation if q.source else None, "origin": q.origin, "status": q.status,
            "risk_level": q.risk_level, "risk_note": q.risk_note, "verifier_note": q.verifier_note}


def list_questions(db: Session, status: str | None = None, area_id: str | None = None) -> list[dict]:
    query = select(Question).join(Subtopic).order_by(Subtopic.order, Question.level, Question.id)
    if status:
        query = query.where(Question.status == status)
    if area_id:
        query = query.where(Subtopic.area_id == area_id)
    return [_question_payload(q) for q in db.scalars(query).all()]


def generate_question(db: Session, subtopic_id: str, level: int, approach: str, source_id: str | None) -> dict:
    status = ai.ai_status()
    if not status["generator"]:
        raise DiagnosticError("La generación con IA no está configurada. Define AI_BASE_URL, AI_API_KEY y "
                              "AI_MODEL_GENERATOR en el archivo .env.", 503)
    subtopic = db.get(Subtopic, subtopic_id)
    if not subtopic:
        raise DiagnosticError("Subtema no encontrado", 404)
    candidates = [s for s in subtopic.sources if s.min_level <= level] or subtopic.sources
    source = db.get(Source, source_id) if source_id else candidates[0]
    if not source or source.subtopic_id != subtopic.id:
        raise DiagnosticError("La fuente no pertenece al subtema (lista cerrada de fuentes)", 400)
    existing = [q.stem for q in subtopic.questions]
    try:
        data = ai.generate_question(subtopic=subtopic, source=source, level=level, approach=approach,
                                    existing_stems=existing)
        check = (ai.verify_question(source=source, stem=data["stem"], options=data["options"],
                                    correct_key=data["correct_key"])
                 if status["verifier"] else {"approved": False, "issues": ["Sin verificador configurado"],
                                             "risk": "alta"})
    except AIUnavailable as exc:
        raise DiagnosticError(str(exc), 502) from exc
    risk = check["risk"] if check["risk"] != "ninguno" else "media"  # generada por IA: nunca riesgo cero
    question = Question(
        subtopic_id=subtopic.id, level=level, approach=approach, kind="mc", concept=data.get("concept", ""),
        stem=data["stem"], options=data["options"], correct_key=data["correct_key"], source_id=source.id,
        origin="ia", status="pendiente" if check["approved"] else "rechazada", risk_level=risk,
        risk_note="Generada por IA a partir de la fuente; requiere aprobación docente.",
        verifier_note="; ".join(check["issues"]) or "El verificador no encontró problemas.")
    db.add(question)
    db.commit()
    db.refresh(question)
    return _question_payload(question)


def decide_question(db: Session, question_id: str, decision: str, note: str) -> dict:
    question = db.get(Question, question_id)
    if not question:
        raise DiagnosticError("Pregunta no encontrada", 404)
    question.status = decision
    if note:
        question.verifier_note = (question.verifier_note + " | Docente: " + note).strip(" |")
    db.commit()
    return _question_payload(question)


def risk_inventory(db: Session) -> dict:
    subs = db.scalars(select(Subtopic).where(Subtopic.risk_level != "ninguno").order_by(Subtopic.area_id)).all()
    srcs = db.scalars(select(Source)).all()
    qs = db.scalars(select(Question).where(Question.risk_level != "ninguno")).all()
    return {
        "subtopics": [{"id": s.id, "area_id": s.area_id, "name": s.name, "risk_level": s.risk_level,
                       "note": s.risk_note} for s in subs],
        "sources": [{"id": s.id, "citation": s.citation, "subtopic_id": s.subtopic_id, "url": s.url,
                     "risk_level": s.risk_level, "note": s.risk_note, "verified_on": s.verified_on}
                    for s in srcs if s.risk_level != "ninguno" or not s.verified_on],
        "questions": [{"id": q.id, "subtopic_id": q.subtopic_id, "level": q.level, "risk_level": q.risk_level,
                       "note": q.risk_note, "status": q.status} for q in qs],
    }
