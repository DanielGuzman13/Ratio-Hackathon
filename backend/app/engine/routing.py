"""Momentos 4 y 5 · Diseñar la ruta y justificar/marcar cada elemento.

Cada elemento de la ruta lleva: subtema, objetivo, prioridad, horas y semana,
fuentes sugeridas, la evidencia del diagnóstico que lo generó y las marcas que
obligan a la revisión del docente. La ruta orienta: no contiene contenido
formativo, solo qué estudiar, por qué y con qué fuentes.
"""
from __future__ import annotations

import math

HOURS_BY_STATUS = {"vacio": 4, "en_desarrollo": 2, "fortaleza": 1}
BASE_PRIORITY = {"vacio": 3.0, "en_desarrollo": 2.0, "fortaleza": 0.5}


def _priority_label(score: float) -> str:
    return "alta" if score >= 3 else ("media" if score >= 1.5 else "baja")


def _blocking(subtopic_id: str, prerequisites: dict[str, list[str]], weak: set[str]) -> list[str]:
    """Subtemas débiles que dependen de este: si está flojo, bloquea su avance."""
    return [sid for sid, pres in prerequisites.items() if subtopic_id in pres and sid in weak]


def _select_sources(sources: list[dict], target_level: int, approach: str) -> list[dict]:
    pool = [s for s in sources if s["min_level"] <= target_level] or sources[:1]
    if approach == "practico":
        pool.sort(key=lambda s: (s["kind"] == "jurisprudencia", s["priority"]))
    else:
        pool.sort(key=lambda s: s["priority"])
    return pool[:3]


def _justification(name: str, result: dict, evidence: list[dict], blocking_names: list[str],
                   probed_by: str | None) -> dict:
    parts = [f"Respondió {result['correct']} de {result['answered']} preguntas de «{name}» "
             f"(dominio ponderado {round((result['mastery'] or 0) * 100)} %, nivel alcanzado: {result['level_name']})."]
    if result["failed_concepts"]:
        parts.append("Falló en: " + ", ".join(result["failed_concepts"]) + ".")
    if blocking_names:
        parts.append("Es prerrequisito de: " + ", ".join(blocking_names) + ", por eso va primero.")
    if probed_by:
        parts.append(f"Se evaluó como posible raíz de los errores en «{probed_by}».")
    return {
        "text": " ".join(parts),
        "errors": result["misconceptions"],
        "evidence": [{"question_id": e["question_id"], "level": e["level"], "concept": e.get("concept", ""),
                      "correct": e["correct"], "score": e["score"]} for e in evidence],
    }


def _quote(stem: str, question_id: str) -> str:
    stem = " ".join((stem or "").split())
    if not stem:
        return f"Pregunta {question_id}."
    return f"«{stem[:180]}{'…' if len(stem) > 180 else ''}»"


def _flags(subtopic: dict, sources: list[dict], evidence: list[dict], result: dict) -> list[dict]:
    flags: list[dict] = []
    if subtopic.get("risk_level", "ninguno") != "ninguno":
        flags.append({"type": "subtema_en_transicion", "severity": subtopic["risk_level"],
                      "text": subtopic["risk_note"]})
    for src in sources:
        if src.get("risk_level", "ninguno") != "ninguno":
            flags.append({"type": "fuente_con_riesgo", "severity": src["risk_level"],
                          "text": f"{src['citation']}: {src['risk_note']}"})
        if not src.get("verified_on"):
            flags.append({"type": "fuente_sin_verificar", "severity": "baja",
                          "text": f"Confirmar enlace y vigencia de: {src['citation']}"})
    for e in evidence:
        quoted = _quote(e.get("stem", ""), e["question_id"])
        if e.get("origin") == "ia":
            # Una sola marca por pregunta de IA (antes salían dos: «con riesgo» y «generada por IA»).
            when = "durante este diagnóstico, a la medida del perfil" if e.get("tailored") else "con IA"
            severity = "alta" if e.get("risk_level") == "alta" else "media"
            flags.append({"type": "pregunta_generada_por_ia", "severity": severity, "question_id": e["question_id"],
                          "text": f"{quoted} Creada {when}. Confirmar que la respuesta correcta y la fuente sean exactas."})
        elif e.get("risk_level", "ninguno") != "ninguno":
            flags.append({"type": "pregunta_con_riesgo", "severity": e["risk_level"], "question_id": e["question_id"],
                          "text": f"{quoted} {e.get('risk_note', '')}".strip()})
        if e.get("grader") == "rubrica":
            flags.append({"type": "calificacion_automatica", "severity": "media", "question_id": e["question_id"],
                          "text": f"Respuesta abierta a {quoted} calificada por palabras clave."})
    if result["confidence"] == "baja":
        flags.append({"type": "evidencia_insuficiente", "severity": "media",
                      "text": "Solo una respuesta respalda esta clasificación."})
    # Una marca por texto: evita duplicados en la vista del docente.
    unique: dict[str, dict] = {}
    for flag in flags:
        unique.setdefault(flag["type"] + flag["text"], flag)
    return list(unique.values())


def build_route(*, per_subtopic: dict[str, dict], subtopics: dict[str, dict], sources: dict[str, list[dict]],
                evidence: dict[str, list[dict]], prerequisites: dict[str, list[str]], approach: str,
                interests: list[str], hours_per_week: int, weeks: int, probed_by: dict[str, str | None],
                order: list[str]) -> list[dict]:
    weak = {sid for sid, r in per_subtopic.items() if r["status"] in ("vacio", "en_desarrollo")}
    candidates = []
    for sid, result in per_subtopic.items():
        if result["status"] == "sin_evidencia":
            continue
        # Solo un subtema débil "bloquea": una fortaleza no necesita ir primero.
        blocks = _blocking(sid, prerequisites, weak) if sid in weak else []
        score = BASE_PRIORITY[result["status"]] + (1.0 if blocks else 0) + (0.5 if sid in interests else 0)
        candidates.append((sid, result, blocks, score))

    # Orden de la ruta:
    # 1) lo débil que bloquea otros subtemas débiles (la raíz del problema),
    # 2) el resto de lo débil: vacíos antes que "en desarrollo", respetando el temario,
    # 3) al final, profundización de fortalezas (priorizando los intereses del usuario).
    status_rank = {"vacio": 0, "en_desarrollo": 1}
    weak_items = [c for c in candidates if c[1]["status"] in status_rank]
    strong_items = [c for c in candidates if c[1]["status"] == "fortaleza"]
    weak_items.sort(key=lambda c: (0 if c[2] else 1, status_rank[c[1]["status"]], order.index(c[0])))
    strong_items.sort(key=lambda c: (-c[3], order.index(c[0])))
    ordered = weak_items + strong_items

    hours_per_week = max(1, hours_per_week)
    capacity = hours_per_week * max(1, weeks)
    used = 0.0
    route = []
    for step, (sid, result, blocks, score) in enumerate(ordered, start=1):
        sub = subtopics[sid]
        hours = HOURS_BY_STATUS[result["status"]] * (1.5 if approach == "tecnico" and result["status"] == "vacio" else 1)
        start_week = int(used // hours_per_week) + 1
        used += hours
        end_week = math.ceil(used / hours_per_week)
        target_level = min(3, result["level_reached"] + 1)
        chosen_sources = _select_sources(sources[sid], target_level, approach)
        names = {k: v["name"] for k, v in subtopics.items()}
        probed = probed_by.get(sid)
        route.append({
            "step": step,
            "subtopic_id": sid,
            "subtopic": sub["name"],
            "status": result["status"],
            "priority": _priority_label(score),
            "priority_score": round(score, 2),
            "objective": sub["objective_practico"] if approach == "practico" else sub["objective_tecnico"],
            "action": "Profundizar" if result["status"] == "fortaleza" else "Reforzar",
            "focus_concepts": result["failed_concepts"] or sub.get("concepts", [])[:2],
            "target_level": target_level,
            "hours": hours,
            "weeks": [start_week, end_week],
            "within_time": used <= capacity,
            "sources": [{k: s[k] for k in ("id", "kind", "citation", "detail", "url", "verified_on",
                                           "risk_level", "risk_note")} for s in chosen_sources],
            "justification": _justification(sub["name"], result, evidence[sid], [names[b] for b in blocks],
                                            names.get(probed) if probed else None),
            "review_flags": _flags(sub, chosen_sources, evidence[sid], result),
        })
    names = {k: v["name"] for k, v in subtopics.items()}
    for item in route:
        sid = item["subtopic_id"]
        if item["status"] == "fortaleza":
            gaps = [p for p in prerequisites.get(sid, []) if per_subtopic.get(p, {}).get("status") == "vacio"]
            if gaps:
                item["review_flags"].append({
                    "type": "resultado_inconsistente", "severity": "media",
                    "text": "Aparece como fortaleza pero su prerrequisito " + ", ".join(f"«{names[g]}»" for g in gaps)
                            + " es un vacío: posible acierto por azar o vacío mal medido."})
        item["requires_review"] = any(f["severity"] in ("media", "alta") for f in item["review_flags"])
    return route
