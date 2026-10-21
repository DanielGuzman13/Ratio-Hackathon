"""Carga del banco semilla (data/seed/*.json) en la base de datos.

Es idempotente: actualiza áreas, subtemas, fuentes y preguntas semilla por id,
sin tocar las preguntas generadas por IA ni las sesiones de usuarios. Así, el
docente o el equipo pueden corregir el JSON y volver a cargarlo.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from .config import settings
from .models import Area, Question, Source, Subtopic

_SUBTOPIC_FIELDS = ("name", "module", "order", "prerequisites", "objective_tecnico",
                    "objective_practico", "concepts", "risk_level", "risk_note")
_SOURCE_FIELDS = ("kind", "citation", "detail", "url", "fragment", "min_level", "priority",
                  "verified_on", "risk_level", "risk_note")
_QUESTION_FIELDS = ("level", "approach", "kind", "concept", "stem", "options", "correct_key",
                    "rubric", "source_id", "risk_level", "risk_note")


def _upsert(db: Session, model, obj_id: str, data: dict, fields: tuple[str, ...], **extra):
    obj = db.get(model, obj_id)
    if obj is None:
        obj = model(id=obj_id)
        db.add(obj)
    for field in fields:
        if field in data:
            setattr(obj, field, data[field])
    for key, value in extra.items():
        setattr(obj, key, value)
    return obj


ARTICLE_REF = re.compile(r"\bart(?:ículo|iculo|\.)s?\s*\d+", re.I)
NORM_NAME = re.compile(r"Constituci|\bCST\b|C[óo]digo|\bLey \d|Decreto|Sentencia|\bSU-|\bC-\d|\bT-\d|Convenio", re.I)


WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = {m: i for i, m in enumerate(["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                                       "septiembre", "octubre", "noviembre", "diciembre"], start=1)}
DATED_WEEKDAY = re.compile(r"\b(lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo)\s+(\d{1,2})\s+de\s+"
                           r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)"
                           r"(?:\s+de\s+(\d{4}))", re.I)


def wrong_weekday_dates(*texts: str) -> list[str]:
    """Fechas con día de la semana que no corresponde (p. ej. «domingo 10 de julio de 2026», que fue viernes)."""
    import datetime as _dt
    import unicodedata as _ud

    def plain(t: str) -> str:
        return _ud.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()

    bad = []
    for text in texts:
        for wd, day, month, year in DATED_WEEKDAY.findall(text or ""):
            try:
                real = _dt.date(int(year), MONTHS[month.lower()], int(day)).weekday()
            except ValueError:
                bad.append(f"{wd} {day} de {month} de {year}")
                continue
            if plain(WEEKDAYS[real]) != plain(wd):
                bad.append(f"{wd} {day} de {month} de {year} (fue {WEEKDAYS[real]})")
    return bad


def apply_corrections(db: Session, path: Path | None = None) -> int:
    """Aplica correcciones jurídicas puntuales (data/review/correcciones.json) una sola vez por pregunta."""
    path = path or settings.seed_dir.parent / "review" / "correcciones.json"
    if not path.exists():
        return 0
    marker = "[Revisión jurídica]"
    applied = 0
    for item in json.loads(path.read_text(encoding="utf-8")).get("preguntas", []):
        q = db.get(Question, item["id"])
        if q is None or marker in (q.verifier_note or ""):
            continue
        q.status = item["status"]
        q.verifier_note = f"{marker} {item['nota']} {q.verifier_note or ''}".strip()[:2000]
        applied += 1
    if applied:
        db.commit()
    return applied


def unnamed_article_refs(stem: str, options: list[dict]) -> list[str]:
    """Textos que citan un artículo sin decir de qué norma es (ni en el texto ni en el enunciado)."""
    if NORM_NAME.search(stem):
        return []
    texts = [stem] + [o.get("text", "") for o in options]
    return [t for t in texts if ARTICLE_REF.search(t) and not NORM_NAME.search(t)]


def validate_seed(payload: dict) -> list[str]:
    """Reglas de calidad del banco: devuelve la lista de errores encontrados."""
    errors: list[str] = []
    subtopic_ids = {s["id"] for s in payload["subtopics"]}
    for sub in payload["subtopics"]:
        source_ids = {src["id"] for src in sub.get("sources", [])}
        for pre in sub.get("prerequisites", []):
            if pre not in subtopic_ids:
                errors.append(f"{sub['id']}: prerrequisito desconocido {pre}")
        if not source_ids:
            errors.append(f"{sub['id']}: el subtema no tiene fuentes")
        for q in sub.get("questions", []):
            qid = q["id"]
            if q.get("source_id") not in source_ids:
                errors.append(f"{qid}: la pregunta debe citar una fuente de su subtema")
            if unnamed_article_refs(q.get("stem", ""), q.get("options", [])):
                errors.append(f"{qid}: cita un artículo sin indicar de qué norma es")
            if wrong_weekday_dates(q.get("stem", ""), *[o.get("text", "") for o in q.get("options", [])]):
                errors.append(f"{qid}: una fecha no coincide con su día de la semana")
            if q.get("level") not in (1, 2, 3):
                errors.append(f"{qid}: nivel inválido")
            if q.get("kind", "mc") == "mc":
                keys = [o["key"] for o in q.get("options", [])]
                if q.get("correct_key") not in keys:
                    errors.append(f"{qid}: la clave correcta no está entre las opciones")
                for opt in q.get("options", []):
                    wrong = opt["key"] != q.get("correct_key")
                    if wrong and not opt.get("misconception"):
                        errors.append(f"{qid}: la opción {opt['key']} no declara el error que diagnostica")
            elif not q.get("rubric"):
                errors.append(f"{qid}: pregunta abierta sin rúbrica")
    return errors


def load_file(db: Session, path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_seed(payload)
    if errors:
        raise ValueError(f"Banco inválido en {path.name}:\n- " + "\n- ".join(errors))

    area_data = payload["area"]
    _upsert(db, Area, area_data["id"], area_data, ("name", "description"))
    counts = {"subtemas": 0, "fuentes": 0, "preguntas": 0}
    for sub in payload["subtopics"]:
        _upsert(db, Subtopic, sub["id"], sub, _SUBTOPIC_FIELDS, area_id=area_data["id"])
        counts["subtemas"] += 1
        for src in sub.get("sources", []):
            _upsert(db, Source, src["id"], src, _SOURCE_FIELDS, subtopic_id=sub["id"])
            counts["fuentes"] += 1
        db.flush()
        for q in sub.get("questions", []):
            data = {"kind": "mc", "approach": "ambos", "risk_level": "ninguno", "risk_note": "",
                    "options": [], "rubric": [], "correct_key": ""} | q
            _upsert(db, Question, q["id"], data, _QUESTION_FIELDS, subtopic_id=sub["id"],
                    origin="seed", status="aprobada")
            counts["preguntas"] += 1
    db.commit()
    return {"area": area_data["id"], **counts}


def load_all(db: Session, seed_dir: Path | None = None) -> list[dict]:
    seed_dir = seed_dir or settings.seed_dir
    return [load_file(db, path) for path in sorted(seed_dir.glob("*.json"))]


if __name__ == "__main__":  # python -m app.seed
    from .db import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        for summary in load_all(session):
            print(summary)
