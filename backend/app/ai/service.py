"""Servicios de IA: generar, verificar y calificar.

Flujo de una pregunta generada:
  generador (fragmento de fuente) -> validación de estructura -> verificador independiente
  -> queda 'pendiente' (o 'rechazada') hasta que el docente la apruebe.
Las preguntas generadas nunca entran al diagnóstico sin pasar por el verificador, y
si se usan sin aprobación docente, su evidencia se marca en la ruta.
"""
from __future__ import annotations

import json

from ..config import settings
from ..seed import unnamed_article_refs
from ..engine.grading import OPEN_PASS_THRESHOLD, GradeResult
from . import prompts
from .client import AIUnavailable, GeminiSearchClient, OpenAICompatibleClient, quota_blocked

generator = OpenAICompatibleClient(settings.generator)
verifier = OpenAICompatibleClient(settings.verifier)
searcher = GeminiSearchClient(settings.generator)


def ai_status() -> dict:
    return {
        "generator": generator.enabled,
        "verifier": verifier.enabled,
        "model_generator": settings.generator.model if generator.enabled else None,
        "model_verifier": settings.verifier.model if verifier.enabled else None,
        "live_generation": settings.ai_live and generator.enabled,
        "free_topic": settings.ai_free_topic and generator.enabled,
        "web_search": searcher.enabled,
        "quota_blocked": quota_blocked(),
    }


def _normalize_options(raw) -> list:
    """Los modelos a veces devuelven las opciones como {"A": "texto"} o ["A) texto", ...]; las llevamos a [{key, text}]."""
    if isinstance(raw, dict):
        raw = [{"key": k, **(v if isinstance(v, dict) else {"text": v})} for k, v in raw.items()]
    out = []
    for i, opt in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(opt, str):
            text = opt.strip()
            key = text[0].upper() if len(text) > 2 and text[1] in ").:-" else "ABCD"[i] if i < 4 else "?"
            if len(text) > 2 and text[1] in ").:-":
                text = text[2:].strip()
            opt = {"key": key, "text": text}
        if isinstance(opt, dict):
            opt = dict(opt)
            opt["key"] = str(opt.get("key") or ("ABCD"[i] if i < 4 else "?")).strip().upper()[:1]
            out.append(opt)
    return out


def _validate_generated(data: dict, valid_sources: set[str] | None = None) -> list[str]:
    if not isinstance(data, dict):
        return ["Formato inválido: la pregunta no es un objeto"]
    data["options"] = _normalize_options(data.get("options"))
    errors = []
    if data.get("error"):
        return [f"El generador declinó: {data['error']}"]
    if valid_sources is not None and data.get("source_id") not in valid_sources:
        errors.append("Cita una fuente que no está en la lista cerrada")
    if data.get("level") not in (1, 2, 3, None):
        errors.append("Nivel inválido")
    if unnamed_article_refs(data.get("stem") or "", data.get("options") or []):
        errors.append("Cita un artículo sin decir de qué norma es")
    options = data.get("options") or []
    keys = [o.get("key") for o in options]
    if not data.get("stem"):
        errors.append("Falta el enunciado")
    if sorted(keys) != ["A", "B", "C", "D"]:
        errors.append("Se requieren exactamente cuatro opciones A-D")
    if data.get("correct_key") not in keys:
        errors.append("La clave correcta no corresponde a una opción")
    for opt in options:
        if opt.get("key") != data.get("correct_key") and not opt.get("misconception"):
            errors.append(f"La opción {opt.get('key')} no declara el error que diagnostica")
    return errors


def generate_question(*, subtopic, source, level: int, approach: str, existing_stems: list[str]) -> dict:
    user = prompts.GENERATOR_USER.format(
        subtopic=subtopic.name, concepts=", ".join(subtopic.concepts), level=level,
        level_description=prompts.LEVEL_DESCRIPTIONS[level], approach=approach,
        citation=source.citation, fragment=source.fragment,
        existing=json.dumps(existing_stems[:12], ensure_ascii=False))
    data = generator.chat_json(prompts.GENERATOR_SYSTEM, user, temperature=0.6)
    errors = _validate_generated(data)
    if errors:
        raise AIUnavailable("Pregunta generada inválida: " + "; ".join(errors))
    return data


def verify_question(*, source, stem: str, options: list[dict], correct_key: str) -> dict:
    user = prompts.VERIFIER_USER.format(
        citation=source.citation, fragment=source.fragment, stem=stem,
        options=json.dumps([{"key": o["key"], "text": o["text"]} for o in options], ensure_ascii=False),
        correct_key=correct_key)
    data = verifier.chat_json(prompts.VERIFIER_SYSTEM, user, temperature=0)
    return {"approved": bool(data.get("approved")), "issues": data.get("issues", []),
            "risk": data.get("risk", "media") if data.get("risk") in ("ninguno", "media", "alta") else "media"}


class AIRubricGrader:
    name = "ia"

    def grade(self, question, answer: str) -> GradeResult:
        rubric = [{"id": r["id"], "criterio": r["criterion"]} for r in question.rubric]
        user = prompts.GRADER_USER.format(stem=question.stem, rubric=json.dumps(rubric, ensure_ascii=False),
                                          answer=answer[:4000])
        data = verifier.chat_json(prompts.GRADER_SYSTEM, user, temperature=0)
        valid_ids = {r["id"] for r in question.rubric}
        hits = [h for h in data.get("hits", []) if h in valid_ids]
        score = len(hits) / (len(valid_ids) or 1)
        missing = [r["criterion"] for r in question.rubric if r["id"] not in hits]
        return GradeResult(score=score, correct=score >= OPEN_PASS_THRESHOLD,
                           misconception=("No desarrolló: " + "; ".join(missing)) if missing else "",
                           rubric_hits=hits, grader=self.name, note=data.get("missing_summary", ""))


# ---------------------------------------------------------------------------
# Generación en vivo por lotes
# ---------------------------------------------------------------------------

def _distribution(count: int) -> tuple[str, list[int]]:
    """Reparte niveles: 1/3 básico, 1/3 intermedio, 1/3 avanzado (mínimo uno de cada uno)."""
    levels = [1 + (i % 3) for i in range(count)]
    text = ", ".join(f"{levels.count(n)} de nivel {n}" for n in (1, 2, 3) if levels.count(n))
    return text + "; mezcla enfoques práctico y técnico", levels


def generate_batch(*, subtopic_name: str, concepts: list[str], sources: list[dict], count: int,
                   existing_stems: list[str], levels: list[int] | None = None, approach: str | None = None,
                   profile: str = "", focus_errors: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """Devuelve (preguntas_válidas, errores). Cada fuente: {id, citation, fragment}.

    Con `levels`, `approach`, `profile` y `focus_errors` la IA genera preguntas a la medida de la persona:
    el nivel y el enfoque que le corresponden y, si ya mostró errores, preguntas que los confirmen o descarten.
    """
    if levels:
        distribution = ", ".join(f"{levels.count(n)} de nivel {n}" for n in (1, 2, 3) if levels.count(n))
    else:
        distribution, _ = _distribution(count)
    if approach in ("practico", "tecnico"):
        distribution += (f"; todas con enfoque '{approach}'"
                         + (" (casos cotidianos en lenguaje sencillo, sin tecnicismos)" if approach == "practico"
                            else " (lenguaje jurídico preciso, artículos y reglas)"))
    source_text = "\n".join(f'- id: {s["id"]} | {s["citation"]}\n  Fragmento: """{s["fragment"]}"""' for s in sources)
    user = prompts.BATCH_GENERATOR_USER.format(
        subtopic=subtopic_name, concepts=", ".join(concepts), count=count, distribution=distribution,
        sources=source_text, existing=json.dumps(existing_stems[-25:], ensure_ascii=False))
    if profile:
        user += f"\nPerfil de la persona evaluada: {profile}"
    if focus_errors:
        user += ("\nErrores que la persona ya mostró en este subtema (diseña preguntas que permitan confirmarlos o "
                 "descartarlos, usando esos errores como distractores cuando el fragmento lo permita): "
                 + "; ".join(focus_errors[:4]))
    data = generator.chat_json(prompts.BATCH_GENERATOR_SYSTEM, user, temperature=0.7)
    valid_ids = {s["id"] for s in sources}
    good, errors = [], []
    seen = {s.strip().lower() for s in existing_stems}
    raw = data.get("questions") if isinstance(data, dict) else data
    for i, q in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(q, dict):
            errors.append(f"Pregunta {i + 1}: formato inválido (no es un objeto)")
            continue
        q.setdefault("level", (levels or [1])[min(i, len(levels or [1]) - 1)])
        q["approach"] = q.get("approach") if q.get("approach") in ("practico", "tecnico", "ambos") else "ambos"
        if approach in ("practico", "tecnico"):
            q["approach"] = approach
        problems = _validate_generated(q, valid_ids)
        if (q.get("stem") or "").strip().lower() in seen:
            problems.append("Pregunta repetida")
        if problems:
            errors.append(f"Pregunta {i + 1}: " + "; ".join(problems))
            continue
        seen.add(q["stem"].strip().lower())
        good.append(q)
    return good, errors


def verify_batch(questions: list[dict], fragments: dict[str, dict]) -> list[dict]:
    """Verificador independiente. Devuelve un resultado por pregunta (mismo orden)."""
    if not questions:
        return []
    items = []
    for i, q in enumerate(questions):
        src = fragments[q["source_id"]]
        items.append({"index": i, "fuente": src["citation"], "fragmento": src["fragment"], "pregunta": q["stem"],
                      "opciones": [{"key": o["key"], "text": o["text"]} for o in q["options"]],
                      "correcta": q["correct_key"]})
    data = verifier.chat_json(prompts.BATCH_VERIFIER_SYSTEM,
                              prompts.BATCH_VERIFIER_USER.format(items=json.dumps(items, ensure_ascii=False, indent=1)),
                              temperature=0)
    results = data.get("results") if isinstance(data, dict) else data
    by_index = {r.get("index"): r for r in (results if isinstance(results, list) else []) if isinstance(r, dict)}
    out = []
    for i in range(len(questions)):
        r = by_index.get(i)
        if r is None:
            out.append({"approved": False, "issues": ["El verificador no evaluó esta pregunta"], "risk": "alta"})
        else:
            risk = r.get("risk") if r.get("risk") in ("ninguno", "media", "alta") else "media"
            issues = r.get("issues") or []
            out.append({"approved": bool(r.get("approved")), "risk": risk,
                        "issues": [issues] if isinstance(issues, str) else [str(x) for x in issues]})
    return out


def build_topic_map(topic: str, profile_summary: str) -> tuple[dict, list[dict], bool, str]:
    """Mapa de subtemas y fuentes para un tema libre.

    Devuelve (mapa, fuentes_web, uso_busqueda, motivo_si_la_busqueda_fallo).
    """
    user = prompts.TOPIC_MAP_USER.format(topic=topic, profile=profile_summary)
    search_error = ""
    if searcher.enabled:
        try:
            data, web = searcher.search_json(prompts.TOPIC_MAP_SYSTEM, user)
            return data, web, True, ""
        except AIUnavailable as exc:
            search_error = str(exc)[:300]  # sin búsqueda: se genera sin web y se marca riesgo alto
    data = generator.chat_json(prompts.TOPIC_MAP_SYSTEM, user, temperature=0.3)
    return data, [], False, search_error
