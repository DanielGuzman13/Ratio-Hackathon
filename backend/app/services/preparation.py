"""Preparación con IA al iniciar un diagnóstico (corre en segundo plano).

Dos modos:
* Área de la lista (laboral, constitucional): por cada subtema, la IA genera un lote de
  preguntas SOLO desde los fragmentos de las fuentes oficiales cargadas, y un verificador
  independiente las revisa. Las aprobadas quedan "verificadas" y entran al diagnóstico.
* Tema libre: primero la IA arma el mapa del tema (subtemas, prerrequisitos, objetivos) y
  busca fuentes oficiales con Google Search; luego genera y verifica como en el otro modo.

Es progresivo: en cuanto el primer subtema está listo la persona empieza a responder, y el
resto se genera mientras tanto. Todo queda en caché: el siguiente usuario del mismo tema no espera.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import func, select

from ..ai import service as ai
from ..ai.client import AIUnavailable, quota_blocked
from ..config import settings
from ..db import SessionLocal
from ..models import Area, DiagnosticSession, GenerationJob, Question, Source, Subtopic

_job_lock = threading.Lock()
TARGET_STATUSES = ("verificada", "aprobada")
RISK_ORDER = ["ninguno", "media", "alta"]


def slugify(text: str, size: int = 32) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:size] or "tema"


def ai_live_enabled() -> bool:
    # Con la cuota diaria agotada no se crean trabajos: el diagnóstico usa el banco guardado.
    return settings.ai_live and ai.generator.enabled and not quota_blocked()


# ---------------------------------------------------------------------------
# Estado del trabajo (pasos visibles para el usuario)
# ---------------------------------------------------------------------------

def _update_job(job_id: str, **changes) -> None:
    with _job_lock, SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        steps = [dict(s) for s in job.steps]
        if "step" in changes:
            key, values = changes.pop("step")
            for step in steps:
                if step["key"] == key:
                    step.update(values)
            job.steps = steps
        if "add_steps" in changes:
            job.steps = steps + changes.pop("add_steps")
        if "done_subtopic" in changes:
            sid = changes.pop("done_subtopic")
            job.pending_subtopics = [s for s in job.pending_subtopics if s != sid]
        if "stats" in changes:
            merged = dict(job.stats or {})
            for k, v in changes.pop("stats").items():
                merged[k] = merged.get(k, 0) + v if isinstance(v, (int, float)) else v
            job.stats = merged
        for k, v in changes.items():
            setattr(job, k, v)
        job.updated_at = datetime.now(timezone.utc)
        db.commit()


def _step(key: str, label: str, status: str = "pendiente") -> dict:
    return {"key": key, "label": label, "status": status, "detail": "", "seconds": None}


# ---------------------------------------------------------------------------
# Generación de un subtema
# ---------------------------------------------------------------------------

def _verified_count(db, subtopic_id: str) -> int:
    return db.scalar(select(func.count()).select_from(Question).where(
        Question.subtopic_id == subtopic_id, Question.origin == "ia", Question.status.in_(TARGET_STATUSES))) or 0


def subtopics_needing_generation(db, area: Area) -> list[str]:
    return [s.id for s in area.subtopics if _verified_count(db, s.id) < settings.ai_questions_per_subtopic]


def _generate_subtopic(job_id: str, subtopic_id: str, risk_floor: str) -> None:
    started = time.monotonic()
    key = f"gen:{subtopic_id}"
    _update_job(job_id, step=(key, {"status": "en_curso"}))
    try:
        if quota_blocked():
            raise AIUnavailable(quota_blocked())
        with SessionLocal() as db:
            sub = db.get(Subtopic, subtopic_id)
            sources = [{"id": s.id, "citation": s.citation, "fragment": s.fragment} for s in sub.sources if s.fragment]
            if not sources:
                raise AIUnavailable("El subtema no tiene fragmentos de fuentes para generar preguntas.")
            existing = [q.stem for q in sub.questions]
            need = max(3, settings.ai_questions_per_subtopic - _verified_count(db, subtopic_id))
            name, concepts = sub.name, list(sub.concepts or [])
        questions, gen_errors = ai.generate_batch(subtopic_name=name, concepts=concepts, sources=sources,
                                                  count=need, existing_stems=existing)
        fragments = {s["id"]: s for s in sources}
        checks = ai.verify_batch(questions, fragments)
        approved = 0
        with SessionLocal() as db:
            for q, check in zip(questions, checks):
                # Generada por IA: nunca riesgo cero; el piso sube a "alta" si no hubo búsqueda web.
                risk = max(check["risk"], risk_floor, key=RISK_ORDER.index)
                status = "verificada" if check["approved"] else "rechazada"
                approved += check["approved"]
                db.add(Question(
                    subtopic_id=subtopic_id, level=int(q["level"]), approach=q["approach"], kind="mc",
                    concept=q.get("concept", "")[:160], stem=q["stem"], options=q["options"],
                    correct_key=q["correct_key"], source_id=q["source_id"], origin="ia", status=status,
                    risk_level=risk, risk_note="Generada por IA en vivo desde la fuente citada; pendiente de revisión docente.",
                    verifier_note="; ".join(check["issues"]) or "El verificador no encontró problemas."))
            db.commit()
        detail = f"{approved} de {len(questions)} preguntas aprobadas por el verificador"
        if gen_errors:
            detail += f" · {len(gen_errors)} descartadas por formato"
        _update_job(job_id, step=(key, {"status": "listo", "detail": detail,
                                        "seconds": round(time.monotonic() - started, 1)}),
                    stats={"generadas": len(questions), "verificadas": approved,
                           "rechazadas": len(questions) - approved, "descartadas": len(gen_errors)})
    except Exception as exc:  # noqa: BLE001 - cualquier fallo de un subtema no debe tumbar los demás
        _update_job(job_id, step=(key, {"status": "error", "detail": str(exc)[:240],
                                        "seconds": round(time.monotonic() - started, 1)}))
    finally:
        _update_job(job_id, done_subtopic=subtopic_id)


def _generate_all(job_id: str, ordered_subtopics: list[str], risk_floor: str) -> None:
    # El primer subtema va solo para que la persona pueda empezar cuanto antes; el resto en paralelo.
    if ordered_subtopics:
        _generate_subtopic(job_id, ordered_subtopics[0], risk_floor)
    with ThreadPoolExecutor(max_workers=max(1, settings.ai_max_concurrency)) as pool:
        list(pool.map(lambda sid: _generate_subtopic(job_id, sid, risk_floor), ordered_subtopics[1:]))


# ---------------------------------------------------------------------------
# Tema libre: construir el área con IA
# ---------------------------------------------------------------------------

def _build_area_from_map(db, area_id: str, topic: str, data: dict, web: list[dict], searched: bool) -> list[str]:
    subtopics = [s for s in (data.get("subtopics") or []) if s.get("name") and s.get("sources")][:5]
    if len(subtopics) < 2:
        raise AIUnavailable("La IA no devolvió un mapa de subtemas utilizable para este tema.")
    area = db.get(Area, area_id)
    area.name = (data.get("name") or topic)[:120]
    area.description = (data.get("description") or f"Tema generado con IA: {topic}")[:500]
    keys = {s.get("key") or f"s{i}": f"{area_id}_{i}" for i, s in enumerate(subtopics)}
    floor = "media" if searched else "alta"
    web_uris = {w["uri"] for w in web}
    for i, s in enumerate(subtopics):
        sid = keys[s.get("key") or f"s{i}"]
        risk = s.get("risk_level") if s.get("risk_level") in ("media", "alta") else floor
        note = s.get("risk_note") or ("Subtema generado por IA con búsqueda web: validar con el docente." if searched
                                      else "Subtema generado por IA SIN búsqueda web: alto riesgo de imprecisión.")
        db.add(Subtopic(
            id=sid, area_id=area_id, name=s["name"][:160], module="Generado con IA", order=i + 1,
            prerequisites=[keys[p] for p in s.get("prerequisites") or [] if p in keys and keys[p] != sid],
            objective_tecnico=s.get("objective_tecnico", ""), objective_practico=s.get("objective_practico", ""),
            concepts=[c for c in s.get("concepts") or []][:6], risk_level=risk, risk_note=note))
        for j, src in enumerate((s.get("sources") or [])[:3]):
            if not src.get("fragment"):
                continue
            url = src.get("url", "")
            verified = url and (url in web_uris or any(d in url for d in (".gov.co", "corteconstitucional", "cortesuprema")))
            db.add(Source(
                id=f"{sid}_f{j}", subtopic_id=sid,
                kind=src.get("kind") if src.get("kind") in ("norma", "jurisprudencia", "guia") else "norma",
                citation=src.get("citation", "Fuente sin título")[:255], url=url[:500],
                detail="Fuente propuesta por la IA" + (" (encontrada con Google Search)" if searched else ""),
                fragment=src["fragment"], min_level=int(src.get("min_level") or 1), priority=j + 1,
                verified_on="", risk_level="alta" if not verified else floor,
                risk_note="Paráfrasis generada por IA: confirmar el texto exacto en la fuente oficial."))
    db.commit()
    return [keys[s.get("key") or f"s{i}"] for i, s in enumerate(subtopics)]


def _init_session_state(session_id: str) -> None:
    from .diagnostic import build_initial_state  # import tardío para evitar ciclo

    with SessionLocal() as db:
        session = db.get(DiagnosticSession, session_id)
        session.state = build_initial_state(db, session.area_id, session.profile)
        db.commit()


def _run_job(job_id: str, session_id: str) -> None:
    try:
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            kind, topic, area_id = job.kind, job.topic, job.area_id
            profile = db.get(DiagnosticSession, session_id).profile
        risk_floor = "media"
        if kind == "tema":
            started = time.monotonic()
            _update_job(job_id, step=("mapa", {"status": "en_curso"}))
            summary = f"{profile.get('perfil')}, objetivo {profile.get('objetivo')}"
            data, web, searched, search_error = ai.build_topic_map(topic, summary)
            with SessionLocal() as db:
                ordered = _build_area_from_map(db, area_id, topic, data, web, searched)
                names = {s.id: s.name for s in db.get(Area, area_id).subtopics}
            risk_floor = "media" if searched else "alta"
            detail = (f"{len(ordered)} subtemas · {len(web)} resultados web consultados" if searched
                      else f"{len(ordered)} subtemas · sin búsqueda web (riesgo alto)"
                      + (f" · la búsqueda falló: {search_error[:120]}" if search_error else ""))
            _update_job(job_id, step=("mapa", {"status": "listo", "detail": detail,
                                               "seconds": round(time.monotonic() - started, 1)}),
                        pending_subtopics=ordered,
                        add_steps=[_step(f"gen:{sid}", f"Generando y verificando: {names[sid]}") for sid in ordered])
            _init_session_state(session_id)
        with SessionLocal() as db:
            order = db.get(DiagnosticSession, session_id).state.get("order", [])
            pending = db.get(GenerationJob, job_id).pending_subtopics
        ordered = [sid for sid in order if sid in pending] + [sid for sid in pending if sid not in order]
        _generate_all(job_id, ordered, risk_floor)
        _update_job(job_id, status="listo")
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, status="error", error=str(exc)[:500])


# ---------------------------------------------------------------------------
# API del servicio
# ---------------------------------------------------------------------------

def create_job(db, *, kind: str, area: Area | None, topic: str = "") -> GenerationJob | None:
    """Crea el trabajo si hace falta generar algo; None si todo está en caché o la IA está apagada."""
    if not ai_live_enabled():
        return None
    if kind == "area":
        needing = subtopics_needing_generation(db, area)
        if not needing:
            return None
        steps = [_step("fuentes", "Fuentes oficiales del área cargadas", "listo")]
        steps += [_step(f"gen:{s.id}", f"Generando y verificando: {s.name}") for s in area.subtopics if s.id in needing]
        job = GenerationJob(area_id=area.id, kind="area", steps=steps, pending_subtopics=needing)
    else:
        label = ("Consultando fuentes oficiales en la web (Google Search) y armando el mapa del tema"
                 if ai.searcher.enabled else "Armando el mapa del tema con IA (sin búsqueda web)")
        job = GenerationJob(area_id=area.id, kind="tema", topic=topic, steps=[_step("mapa", label)],
                            pending_subtopics=[])
    db.add(job)
    db.commit()
    return job


def launch(job_id: str, session_id: str) -> None:
    threading.Thread(target=_run_job, args=(job_id, session_id), daemon=True, name=f"ia-{job_id[:6]}").start()


def job_payload(db, job_id: str | None) -> dict | None:
    if not job_id:
        return None
    job = db.get(GenerationJob, job_id)
    if not job:
        return None
    elapsed = (datetime.now(timezone.utc) - job.created_at.replace(tzinfo=timezone.utc)).total_seconds()
    return {"id": job.id, "kind": job.kind, "status": job.status, "steps": job.steps,
            "pending_subtopics": job.pending_subtopics, "stats": job.stats, "error": job.error,
            "elapsed": round(elapsed, 1)}


def pending_for(db, job_id: str | None) -> set[str]:
    if not job_id:
        return set()
    job = db.get(GenerationJob, job_id)
    if not job or job.status != "en_curso":
        return set()
    return set(job.pending_subtopics)
