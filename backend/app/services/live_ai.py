"""IA adaptativa DURANTE el diagnóstico (en segundo plano, sin hacer esperar a la persona).

Flujo:
1. Al iniciar, la persona responde preguntas del banco elegidas para su perfil y sus temas.
   Al mismo tiempo se lanza el primer lote: la IA genera preguntas para su perfil (nivel y enfoque).
2. Cada AI_BATCH_EVERY respuestas, si no hay un lote en curso, se lanza otro lote que mira
   cómo va la persona: prioriza los subtemas donde falló (con sus errores detectados como foco),
   luego los que aún no se han evaluado y luego los que domina (al nivel siguiente).
3. Lo generado pasa por el verificador independiente; solo lo aprobado entra al banco, marcado
   como generado para esa sesión y ese perfil. El motor lo prefiere en las siguientes preguntas.
4. Todo queda guardado (el banco crece con cada uso) y se actualiza el CSV para revisión docente.

Nunca bloquea: si la IA tarda o falla, el motor sigue con el banco existente.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from ..ai import service as ai
from ..ai.client import quota_blocked
from ..config import settings
from ..db import SessionLocal
from ..engine.profiling import GOAL_LABELS, LEVEL_NAMES, PROFILE_LABELS
from ..models import AIBatch, DiagnosticSession, Question, Subtopic

log = logging.getLogger("ratio.ai")
_trigger_lock = threading.Lock()


def enabled() -> bool:
    return settings.ai_live and ai.generator.enabled and not quota_blocked()


def profile_text(profile: dict, plan: dict) -> str:
    parts = [PROFILE_LABELS.get(profile.get("perfil"), profile.get("perfil", ""))]
    if profile.get("semestre"):
        parts.append(f"{profile['semestre']}.º semestre")
    if profile.get("profesion"):
        parts.append(f"profesión: {profile['profesion']}")
    parts.append(f"objetivo: {GOAL_LABELS.get(profile.get('objetivo'), profile.get('objetivo', ''))}")
    parts.append("enfoque " + ("práctico, para alguien sin formación jurídica" if plan["approach"] == "practico"
                               else "técnico-jurídico"))
    parts.append(f"nivel máximo a evaluar: {LEVEL_NAMES[plan['max_level']]}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Qué generar: lectura del estado del diagnóstico
# ---------------------------------------------------------------------------

def _unused_tailored(db, session_id: str, subtopic_id: str, level: int, asked: set[str]) -> int:
    rows = db.scalars(select(Question.id).where(
        Question.session_id == session_id, Question.subtopic_id == subtopic_id, Question.level == level,
        Question.status == "verificada")).all()
    return sum(1 for qid in rows if qid not in asked)


def _unused_in_bank(db, subtopic_id: str, level: int, asked: set[str]) -> int:
    rows = db.scalars(select(Question.id).where(
        Question.subtopic_id == subtopic_id, Question.status.in_(("aprobada", "verificada")),
        Question.kind == "mc", Question.level.between(level - 1, level + 1))).all()
    return sum(1 for qid in rows if qid not in asked)


def max_batches(session: DiagnosticSession) -> int:
    """Lotes permitidos: crecen con el número de preguntas del diagnóstico (15 preguntas de un tema = 5 lotes)."""
    total = session.state.get("max_questions", 0) if session.state else 0
    return max(settings.ai_max_batches, -(-total // max(1, settings.ai_batch_size)))


def choose_targets(db, session: DiagnosticSession, urgent: str | None = None) -> list[dict]:
    state = session.state
    plan = state["plan"]
    subs = state["subtopics"]
    candidates = []
    for idx, sid in enumerate(state["order"]):
        sub = subs[sid]
        if sub["done"]:
            continue
        level = max(1, min(plan["max_level"], sub["level"]))
        errors = [r["misconception"] for r in sub["results"] if not r["correct"] and r.get("misconception")]
        asked = set(sub["asked"])
        if sid == urgent:
            candidates.append((-1, idx, {"subtopic_id": sid, "name": state["names"][sid], "level": level,
                                         "reason": "Se acabaron las preguntas de este tema: la IA genera más",
                                         "focus": list(dict.fromkeys(errors))[:4]}))
            continue
        if _unused_tailored(db, session.id, sid, level, asked) >= 2:
            continue  # ya tiene preguntas a la medida pendientes de usar
        remaining = state["per_subtopic"] - len(sub["results"])
        if not errors and _unused_in_bank(db, sid, level, asked) >= max(3, remaining):
            continue  # el banco ya alcanza para este tema: se ahorra cuota de IA
        if errors:
            priority, reason = 0, "Falló en este subtema: preguntas para confirmar o descartar sus errores"
        elif not sub["asked"]:
            priority, reason = 1, "Subtema aún sin evaluar: preguntas a la medida de su perfil"
        else:
            priority, reason = 2, f"Acertó: preguntas de nivel {LEVEL_NAMES[level].lower()} para medir hasta dónde llega"
        candidates.append((priority, idx, {"subtopic_id": sid, "name": state["names"][sid], "level": level,
                                           "reason": reason, "focus": list(dict.fromkeys(errors))[:4]}))
    candidates.sort(key=lambda c: (c[0], c[1]))
    picked = [c[2] for c in candidates[:max(1, settings.ai_batch_targets)]]
    if not picked:
        return []
    size = max(1, settings.ai_batch_size)
    for i, target in enumerate(picked):  # reparte el tamaño del lote entre los subtemas elegidos
        target["count"] = size // len(picked) + (1 if i < size % len(picked) else 0)
    return [t for t in picked if t["count"] > 0]


STALE_SECONDS = 300  # un lote «en curso» por más de 5 min se da por perdido (p. ej. la función se cortó)


def _busy(batch: AIBatch) -> bool:
    if batch.status not in ("en_curso", "pendiente"):
        return False
    created = batch.created_at
    if created is None:
        return True
    created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() < STALE_SECONDS


def should_trigger(db, session: DiagnosticSession, urgent: str | None = None) -> bool:
    if not enabled() or not session.state or session.status != "en_curso":
        return False
    batches = db.scalars(select(AIBatch).where(AIBatch.session_id == session.id)
                         .order_by(AIBatch.id.desc())).all()
    if any(_busy(b) for b in batches):
        return False
    if len(batches) >= max_batches(session):
        return False
    if urgent:
        return True
    answered = session.state.get("count", 0)
    last = batches[0].answers_at_trigger if batches else -settings.ai_batch_every
    return answered - last >= settings.ai_batch_every


def maybe_trigger(db, session: DiagnosticSession, urgent: str | None = None) -> AIBatch | None:
    """Lanza un lote en segundo plano si corresponde. Seguro ante solicitudes simultáneas.

    `urgent`: subtema que se quedó sin preguntas; se genera de inmediato para él.
    """
    with _trigger_lock:
        db.refresh(session)
        if not should_trigger(db, session, urgent):
            return None
        targets = choose_targets(db, session, urgent)
        if not targets:
            return None
        batch = AIBatch(session_id=session.id, answers_at_trigger=session.state.get("count", 0), targets=targets,
                        status="pendiente" if settings.serverless else "en_curso")
        db.add(batch)
        db.commit()
        batch_id = batch.id
    if settings.serverless:  # lo ejecuta el navegador con POST /sessions/{id}/ai/run (ver run_pending)
        return batch
    threading.Thread(target=_run_batch, args=(batch_id,), daemon=True, name=f"ia-lote-{batch_id}").start()
    return batch


# ---------------------------------------------------------------------------
# Ejecución del lote (hilo en segundo plano)
# ---------------------------------------------------------------------------

def _run_batch(batch_id: int) -> None:
    started = time.monotonic()
    generated = verified = 0
    errors: list[str] = []
    with SessionLocal() as db:
        batch = db.get(AIBatch, batch_id)
        session = db.get(DiagnosticSession, batch.session_id) if batch else None
        if session is None:
            return
        profile, plan = dict(session.profile), dict(session.state["plan"])
        session_id, targets = session.id, list(batch.targets)
    who = profile_text(profile, plan)
    for target in targets:
        try:
            with SessionLocal() as db:
                sub = db.get(Subtopic, target["subtopic_id"])
                sources = [{"id": s.id, "citation": s.citation, "fragment": s.fragment}
                           for s in sub.sources if s.fragment and s.min_level <= max(target["level"], 1) + 1]
                sources = sources or [{"id": s.id, "citation": s.citation, "fragment": s.fragment}
                                      for s in sub.sources if s.fragment]
                existing = [q.stem for q in sub.questions]
                name, concepts = sub.name, list(sub.concepts or [])
            questions, gen_errors = ai.generate_batch(
                subtopic_name=name, concepts=concepts, sources=sources, count=target["count"],
                existing_stems=existing, levels=[target["level"]] * target["count"], approach=plan["approach"],
                profile=who, focus_errors=target["focus"])
            checks = ai.verify_batch(questions, {s["id"]: s for s in sources})
            with SessionLocal() as db:
                for q, check in zip(questions, checks):
                    risk = check["risk"] if check["risk"] != "ninguno" else "media"
                    db.add(Question(
                        subtopic_id=target["subtopic_id"], level=int(q["level"]), approach=q["approach"], kind="mc",
                        concept=(q.get("concept") or "")[:160], stem=q["stem"], options=q["options"],
                        correct_key=q["correct_key"], source_id=q["source_id"], origin="ia",
                        status="verificada" if check["approved"] else "rechazada", risk_level=risk,
                        risk_note="Generada por IA durante un diagnóstico, a la medida del perfil; pendiente de revisión docente.",
                        verifier_note="; ".join(check["issues"]) or "El verificador no encontró problemas.",
                        session_id=session_id, profile_tag=profile.get("perfil", "")))
                    generated += 1
                    verified += bool(check["approved"])
                db.commit()
            target["discarded"] = len(gen_errors)  # descartadas por formato (no es un error del lote)
        except Exception as exc:  # noqa: BLE001 - un subtema que falla no detiene el resto
            errors.append(f"{target['name']}: {str(exc)[:200]}")
            log.warning("Lote %s, subtema %s: %s", batch_id, target["subtopic_id"], exc)
    with SessionLocal() as db:
        batch = db.get(AIBatch, batch_id)
        if batch is None:  # la sesión o el lote se eliminaron mientras la IA trabajaba
            return
        batch.targets = targets
        batch.generated, batch.verified = generated, verified
        batch.seconds = round(time.monotonic() - started, 1)
        batch.status = "listo" if generated or not errors else "error"
        batch.error = " | ".join(errors)[:1000]
        db.commit()
    export_csv()


def export_csv() -> None:
    """Mantiene actualizado preguntas_exportadas.csv (si está abierto en Excel, se omite sin error)."""
    if not settings.database_url.startswith("sqlite:///"):
        return
    try:
        from pathlib import Path

        from ..ai.exportar import ROOT, export

        db_path = Path(settings.database_url.removeprefix("sqlite:///"))
        export(db_path, ROOT / "preguntas_exportadas.csv", include_curated=False)
    except Exception as exc:  # noqa: BLE001
        log.info("No se actualizó el CSV (¿está abierto en Excel?): %s", exc)


# ---------------------------------------------------------------------------
# Información para la interfaz y el docente
# ---------------------------------------------------------------------------

def refillable(db, session: DiagnosticSession) -> set[str]:
    """Subtemas para los que vale la pena esperar a la IA si se quedan sin preguntas."""
    if not enabled() or not session.state:
        return set()
    batches = db.scalars(select(AIBatch).where(AIBatch.session_id == session.id)).all()
    if not any(_busy(b) for b in batches) and len(batches) >= max_batches(session):
        return set()
    # Si los últimos lotes fallaron, no se espera indefinidamente.
    recent = sorted(batches, key=lambda b: b.id)[-2:]
    if len(recent) == 2 and all(b.status == "error" for b in recent):
        return set()
    return {sid for sid, sub in session.state["subtopics"].items() if not sub["done"]}


def session_ai_status(db, session_id: str) -> dict:
    batches = db.scalars(select(AIBatch).where(AIBatch.session_id == session_id)).all()
    tailored = db.scalar(select(func.count()).select_from(Question).where(
        Question.session_id == session_id, Question.status == "verificada")) or 0
    return {"enabled": enabled(), "active": any(_busy(b) for b in batches),
            "pending": any(b.status == "pendiente" and _busy(b) for b in batches),
            "batches": len(batches), "tailored_questions": tailored}


def run_pending(db, session_id: str) -> dict:
    """Serverless: ejecuta aquí mismo los lotes pendientes de la sesión (el navegador lo pide sin esperar).

    El cambio pendiente -> en_curso es atómico, así que dos solicitudes no generan el mismo lote.
    """
    ran = []
    ids = db.scalars(select(AIBatch.id).where(AIBatch.session_id == session_id, AIBatch.status == "pendiente")).all()
    for batch_id in ids:
        claimed = db.execute(update(AIBatch).where(AIBatch.id == batch_id, AIBatch.status == "pendiente")
                             .values(status="en_curso")).rowcount
        db.commit()
        if claimed:
            _run_batch(batch_id)
            ran.append(batch_id)
    return {"ran": ran}


def batches_payload(db, session_id: str) -> list[dict]:
    rows = db.scalars(select(AIBatch).where(AIBatch.session_id == session_id).order_by(AIBatch.id)).all()
    return [{"id": b.id, "status": b.status, "after_answers": b.answers_at_trigger, "targets": b.targets,
             "generated": b.generated, "verified": b.verified, "seconds": b.seconds, "error": b.error,
             "created_at": b.created_at.isoformat() if b.created_at else None} for b in rows]
