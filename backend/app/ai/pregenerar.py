"""Precalienta el banco: genera y verifica con IA las preguntas de las áreas de la lista.

Uso (desde backend/, con el servidor detenido o encendido):

    python -m app.ai.pregenerar               # todas las áreas
    python -m app.ai.pregenerar laboral       # una sola área

Deja guardadas AI_QUESTIONS_PER_SUBTOPIC preguntas verificadas por subtema. Después, cuando
alguien inicia un diagnóstico de esa área, empieza al instante (no hay que esperar a la IA).
"""
from __future__ import annotations

import sys
import time

from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal, init_db
from ..models import Area, GenerationJob
from ..seed import load_all
from ..services import preparation


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    if not preparation.ai_live_enabled():
        print("✘ La IA no está configurada (AI_BASE_URL, AI_API_KEY, AI_MODEL_GENERATOR en .env).")
        return 1
    init_db()
    with SessionLocal() as db:
        load_all(db)
        query = select(Area).where(Area.origin == "curada")
        if len(sys.argv) > 1:
            query = query.where(Area.id.in_(sys.argv[1:]))
        areas = db.scalars(query).all()
        plan = [(a.id, a.name, preparation.subtopics_needing_generation(db, a), {s.id: s.name for s in a.subtopics})
                for a in areas]
    print(f"Meta: {settings.ai_questions_per_subtopic} preguntas verificadas por subtema · modelo {settings.generator.model}")
    total_start = time.monotonic()
    for area_id, name, needing, names in plan:
        if not needing:
            print(f"\n✔ {name}: ya está listo (en caché).")
            continue
        print(f"\n▶ {name}: generando {len(needing)} subtemas…", flush=True)
        with SessionLocal() as db:
            job = GenerationJob(area_id=area_id, kind="area", pending_subtopics=needing,
                                steps=[preparation._step(f"gen:{sid}", names[sid]) for sid in needing])
            db.add(job)
            db.commit()
            job_id = job.id
        started = time.monotonic()
        preparation._generate_all(job_id, needing, "media")
        preparation._update_job(job_id, status="listo")
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            for step in job.steps:
                mark = {"listo": "✔", "error": "✘"}.get(step["status"], "·")
                print(f"  {mark} {step['label']} ({step['seconds']} s): {step['detail']}")
            stats = job.stats or {}
        print(f"  Total {name}: {time.monotonic() - started:.0f} s · {stats.get('verificadas', 0)} verificadas, "
              f"{stats.get('rechazadas', 0)} rechazadas")
    print(f"\nListo en {time.monotonic() - total_start:.0f} s. Las preguntas se ven en Panel docente → Banco de preguntas.")
    print("Si algún subtema quedó corto (por ejemplo, por límite de cuota), vuelve a correr el comando: solo completa lo que falta.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
