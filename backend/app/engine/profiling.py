"""Momento 1 · Perfilar.

Traduce el cuestionario de perfil en parámetros del diagnóstico: enfoque
(técnico-jurídico o práctico), nivel de arranque y techo de dificultad.
"""
from __future__ import annotations

from dataclasses import dataclass

LEVEL_NAMES = {0: "Sin dominio básico", 1: "Básico", 2: "Intermedio", 3: "Avanzado"}

PROFILE_LABELS = {
    "no_abogado": "Profesional de otra área o persona sin formación jurídica",
    "estudiante": "Estudiante de Derecho",
    "abogado": "Abogado(a)",
}

GOAL_LABELS = {
    "conocer_derechos": "Conocer mis derechos y cómo reclamarlos",
    "examen": "Preparar un examen o parcial",
    "consultorio": "Prepararme para consultorio jurídico",
    "ejercicio": "Ejercicio profesional y litigio",
    "actualizacion": "Actualizarme en reformas recientes",
}


@dataclass(frozen=True)
class DiagnosticPlan:
    approach: str          # tecnico | practico
    start_level: int       # 1..3
    max_level: int         # techo de dificultad
    rationale: list[str]   # por qué se decidió así (explicabilidad)

    def as_dict(self) -> dict:
        return {"approach": self.approach, "start_level": self.start_level,
                "max_level": self.max_level, "rationale": self.rationale}


def build_plan(profile: dict) -> DiagnosticPlan:
    kind = profile.get("perfil", "no_abogado")
    goal = profile.get("objetivo", "")
    semester = profile.get("semestre") or 0
    rationale: list[str] = []

    if kind == "no_abogado" or goal == "conocer_derechos":
        approach = "practico"
        rationale.append("Enfoque práctico: el objetivo es aplicar el derecho a situaciones cotidianas, no el análisis técnico.")
    else:
        approach = "tecnico"
        rationale.append("Enfoque técnico-jurídico: el perfil requiere manejo de normas, términos y jurisprudencia.")

    if kind == "abogado":
        start, ceiling = 2, 3
        rationale.append("Arranca en nivel intermedio por tener formación jurídica completa.")
    elif kind == "estudiante" and semester >= 6:
        start, ceiling = 2, 3
        rationale.append(f"Arranca en nivel intermedio: estudiante de {semester}.º semestre.")
    elif kind == "estudiante":
        start, ceiling = 1, 3
        rationale.append("Arranca en nivel básico: estudiante de primeros semestres.")
    else:
        start, ceiling = 1, 2
        rationale.append("Arranca en nivel básico y llega hasta intermedio: no se evalúa análisis jurisprudencial a quien no es abogado.")

    if goal == "ejercicio" and ceiling < 3:
        ceiling = 3
    return DiagnosticPlan(approach=approach, start_level=start, max_level=ceiling, rationale=rationale)
