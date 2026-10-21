"""Exporta las preguntas a un CSV que se abre directo en Excel (para revisión del equipo jurídico).

Uso (desde backend/):
    python -m app.ai.exportar                 # solo preguntas generadas por IA
    python -m app.ai.exportar --todas         # también las curadas

Crea `preguntas_exportadas.csv` en la carpeta del proyecto. Solo usa la biblioteca estándar,
así que también funciona sin el entorno virtual: python backend/app/ai/exportar.py
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

COLUMNS = ["Área", "Subtema", "Nivel", "Enfoque", "Origen", "Estado", "Concepto", "Pregunta",
           "Opción A", "Opción B", "Opción C", "Opción D", "Correcta",
           "Error que diagnostica A", "Error que diagnostica B", "Error que diagnostica C", "Error que diagnostica D",
           "Fuente", "Riesgo", "Nota del verificador", "Generada para el perfil", "Generada en la sesión",
           "Veces mostrada", "% de acierto", "ID"]
PROFILES = {"estudiante": "Estudiante de Derecho", "abogado": "Abogado(a)", "no_abogado": "Otra profesión"}
LEVELS = {1: "Básico", 2: "Intermedio", 3: "Avanzado"}


def _get(row: sqlite3.Row, key: str, default=""):
    return row[key] if key in row.keys() and row[key] is not None else default


def _rate(row: sqlite3.Row) -> str:
    shown = _get(row, "times_shown", 0)
    return f"{100 * _get(row, 'times_correct', 0) / shown:.0f} %" if shown else ""


def export(db_path: Path, out_path: Path, include_curated: bool) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    where = "" if include_curated else "WHERE q.origin = 'ia'"
    rows = con.execute(f"""
        SELECT a.name AS area, s.name AS subtema, q.*, src.citation AS fuente
        FROM questions q JOIN subtopics s ON s.id = q.subtopic_id JOIN areas a ON a.id = s.area_id
        LEFT JOIN sources src ON src.id = q.source_id
        {where}
        ORDER BY a.name, s."order", q.level, q.status, q.created_at""").fetchall()
    # utf-8-sig + punto y coma: Excel en español lo abre bien con doble clic.
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(COLUMNS)
        for r in rows:
            options = {o["key"]: o for o in json.loads(r["options"] or "[]")}
            rubric = json.loads(r["rubric"] or "[]")
            texts = [options.get(k, {}).get("text", "") for k in "ABCD"]
            errors = [options.get(k, {}).get("misconception", "") for k in "ABCD"]
            if r["kind"] == "open":
                texts = ["(pregunta abierta) Rúbrica: " + " | ".join(i["criterion"] for i in rubric), "", "", ""]
            writer.writerow([r["area"], r["subtema"], LEVELS.get(r["level"], r["level"]), r["approach"],
                             "IA" if r["origin"] == "ia" else "Curada", r["status"], r["concept"], r["stem"],
                             *texts, r["correct_key"], *errors, r["fuente"] or "", r["risk_level"],
                             r["verifier_note"], PROFILES.get(_get(r, "profile_tag"), ""), _get(r, "session_id"),
                             _get(r, "times_shown", 0), _rate(r), r["id"]])
    return len(rows)


def main() -> int:
    db_path = ROOT / "ratio.db"
    if not db_path.exists():
        print(f"✘ No encontré la base de datos en {db_path}")
        return 1
    out = ROOT / "preguntas_exportadas.csv"
    n = export(db_path, out, "--todas" in sys.argv)
    print(f"✔ {n} preguntas exportadas a {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
