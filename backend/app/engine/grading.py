"""Calificación de respuestas (patrón Strategy).

* KeyGrader: opción múltiple; si falla, devuelve el error conceptual asociado a
  la opción elegida (cada distractor diagnostica un error distinto).
* RubricKeywordGrader: preguntas abiertas sin IA; revisa qué criterios de la
  rúbrica aparecen. Es la opción gratuita y determinista.
* La calificación con IA vive en app.ai.service y produce el mismo GradeResult.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

OPEN_PASS_THRESHOLD = 0.6


@dataclass
class GradeResult:
    score: float
    correct: bool
    misconception: str = ""
    rubric_hits: list[str] = field(default_factory=list)
    grader: str = "clave"
    note: str = ""


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text.lower()).strip()


class KeyGrader:
    name = "clave"

    def grade(self, question, answer: str) -> GradeResult:
        chosen = (answer or "").strip().upper()
        if chosen == question.correct_key:
            return GradeResult(score=1.0, correct=True, grader=self.name)
        option = next((o for o in question.options if o["key"] == chosen), None)
        misconception = option.get("misconception", "") if option else "Respuesta no válida"
        return GradeResult(score=0.0, correct=False, misconception=misconception, grader=self.name)


class RubricKeywordGrader:
    name = "rubrica"

    def grade(self, question, answer: str) -> GradeResult:
        text = normalize(answer)
        hits = [item["id"] for item in question.rubric
                if any(normalize(k) in text for k in item.get("keywords", []))]
        total = len(question.rubric) or 1
        score = len(hits) / total
        missing = [item["criterion"] for item in question.rubric if item["id"] not in hits]
        misconception = ("No desarrolló: " + "; ".join(missing)) if missing else ""
        return GradeResult(score=score, correct=score >= OPEN_PASS_THRESHOLD, misconception=misconception,
                           rubric_hits=hits, grader=self.name,
                           note="Calificación automática por palabras clave: requiere revisión docente.")
