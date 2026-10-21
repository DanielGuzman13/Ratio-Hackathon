"""Momento 3 · Clasificar.

Convierte la evidencia (respuestas) en un resultado por subtema y global:
dominio ponderado por dificultad, nivel alcanzado, estado (fortaleza, en
desarrollo, vacío), confianza de la medición y errores conceptuales detectados.
"""
from __future__ import annotations

from collections import Counter

from .profiling import LEVEL_NAMES

STATUS_LABELS = {"fortaleza": "Fortaleza", "en_desarrollo": "En desarrollo", "vacio": "Vacío conceptual",
                 "sin_evidencia": "Sin evidencia"}


def classify_subtopic(evidence: list[dict]) -> dict:
    if not evidence:
        return {"status": "sin_evidencia", "mastery": None, "level_reached": 0, "confidence": "nula",
                "answered": 0, "correct": 0, "failed_concepts": [], "misconceptions": []}
    weight = sum(e["level"] for e in evidence)
    mastery = sum(e["score"] * e["level"] for e in evidence) / weight
    correct_levels = [e["level"] for e in evidence if e["correct"]]
    level_reached = max(correct_levels, default=0)
    # Un error en un nivel inferior al alcanzado indica dominio inestable: se baja un nivel.
    if level_reached and any(not e["correct"] and e["level"] < level_reached for e in evidence):
        level_reached -= 1
    # «Fortaleza» exige al menos 3 respuestas: con 2 aciertos el azar (25 % por pregunta) pesa demasiado.
    if mastery >= 0.75 and len(evidence) >= 3:
        status = "fortaleza"
    elif mastery < 0.45:
        status = "vacio"
    else:
        status = "en_desarrollo"
    n = len(evidence)
    confidence = "alta" if n >= 4 else ("media" if n >= 2 else "baja")
    failed = [e for e in evidence if not e["correct"]]
    return {
        "status": status,
        "mastery": round(mastery, 3),
        "level_reached": level_reached,
        "level_name": LEVEL_NAMES[level_reached],
        "confidence": confidence,
        "answered": n,
        "correct": sum(1 for e in evidence if e["correct"]),
        "failed_concepts": list(dict.fromkeys(e["concept"] for e in failed if e.get("concept"))),
        "misconceptions": list(dict.fromkeys(e["misconception"] for e in failed if e.get("misconception"))),
    }


def classify_global(per_subtopic: dict[str, dict], approach: str, max_level: int) -> dict:
    measured = [r for r in per_subtopic.values() if r["status"] != "sin_evidencia"]
    if not measured:
        return {"level": 0, "level_name": LEVEL_NAMES[0], "approach": approach, "mastery": None,
                "status_counts": {}}
    avg_level = sum(r["level_reached"] for r in measured) / len(measured)
    avg_mastery = sum(r["mastery"] for r in measured) / len(measured)
    # El nivel exige a la vez altura (nivel alcanzado) y consistencia (dominio): así no sale
    # «Intermedio» con un dominio del 39 %, por ejemplo.
    if avg_level >= 2.4 and avg_mastery >= 0.7:
        level = 3
    elif avg_level >= 1.5 and avg_mastery >= 0.5:
        level = 2
    elif avg_level >= 0.75 and avg_mastery >= 0.3:
        level = 1
    else:
        level = 0
    level = min(level, max_level)
    counts = Counter(r["status"] for r in measured)
    note = ""
    if approach == "practico" and level >= max_level and avg_mastery >= 0.8:
        note = "Domina el nivel práctico evaluado: puede pasar a un enfoque técnico-jurídico."
    return {
        "level": level,
        "level_name": LEVEL_NAMES[level],
        "approach": approach,
        "approach_label": "Práctico para no abogados" if approach == "practico" else "Técnico-jurídico",
        "mastery": round(avg_mastery, 3),
        "status_counts": dict(counts),
        "ceiling_note": f"Nivel máximo evaluable para este perfil: {LEVEL_NAMES[max_level]}." if max_level < 3 else "",
        "recommendation": note,
    }
