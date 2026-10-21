"""Momento 2 · Diagnosticar: motor de evaluación adaptativa.

Algoritmo de escalera (staircase) por subtema, con exploración de prerrequisitos:

* Cada subtema arranca en el nivel que fija el perfil.
* Acierto  -> sube un nivel (hasta el techo). Acertar en el techo cierra el subtema.
* Error    -> baja un nivel para ubicar el concepto previo que falla.
             Si falla en el nivel básico, abre (o prioriza) el subtema prerrequisito:
             así el diagnóstico encuentra la raíz del vacío y no solo el síntoma.
* Los subtemas se recorren en orden de prerrequisitos y de forma intercalada,
  para que todos reciban evidencia dentro del presupuesto de preguntas.

El estado es un dict serializable (se guarda en JSON en la base de datos) y cada
decisión deja una traza legible que luego justifica la ruta (punto 5 del reto).
Es lógica pura, sin base de datos ni IA: se prueba de forma aislada.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

APPROACH_PREFERENCE = {
    "practico": ("practico", "ambos", "tecnico"),
    "tecnico": ("tecnico", "ambos", "practico"),
}


@dataclass(frozen=True)
class QuestionRef:
    id: str
    subtopic_id: str
    level: int
    approach: str
    kind: str
    origin: str = "seed"  # seed | ia
    fit: int = 2          # ajuste al perfil: 0 generada para esta sesión, 1 mismo perfil, 2 curada, 3 otro perfil
    shown: int = 0        # veces que ya se mostró (para repartir la exposición del banco)


WAIT = "esperar"  # el subtema que sigue aún se está generando con IA


def jitter(seed: int, key: str) -> int:
    """Número pseudoaleatorio estable por sesión: varía entre sesiones, pero no al recargar."""
    return int(hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()[:8], 16)


@dataclass(frozen=True)
class SubtopicRef:
    id: str
    name: str
    prerequisites: tuple[str, ...]
    order: int


def topological_order(subtopics: list[SubtopicRef]) -> list[str]:
    by_id = {s.id: s for s in subtopics}
    seen: set[str] = set()
    out: list[str] = []

    def visit(sid: str) -> None:
        if sid in seen or sid not in by_id:
            return
        seen.add(sid)
        for pre in by_id[sid].prerequisites:
            visit(pre)
        out.append(sid)

    for sub in sorted(subtopics, key=lambda s: s.order):
        visit(sub.id)
    return out


def new_state(subtopics: list[SubtopicRef], plan: dict, interests: list[str],
              max_questions: int, per_subtopic: int, max_open: int = 2, prefer_generated: bool = False,
              seed: int | None = None) -> dict:
    seed = random.randrange(1, 2**31) if seed is None else seed
    order = topological_order(subtopics)
    # Los intereses del usuario se evalúan primero dentro de su mismo nivel de dependencia.
    depth: dict[str, int] = {}
    by_id = {s.id: s for s in subtopics}
    for sid in order:
        pres = [p for p in by_id[sid].prerequisites if p in depth]
        depth[sid] = 1 + max((depth[p] for p in pres), default=-1)
    topo_index = {sid: i for i, sid in enumerate(order)}
    # Se respetan los prerrequisitos (profundidad) y los intereses; dentro de eso, el orden es aleatorio.
    order = sorted(order, key=lambda sid: (depth[sid], sid not in interests, jitter(seed, sid), topo_index[sid]))
    return {
        "order": order,
        "names": {s.id: s.name for s in subtopics},
        "prerequisites": {s.id: list(s.prerequisites) for s in subtopics},
        "plan": plan,
        "max_questions": max_questions,
        "per_subtopic": per_subtopic,
        "max_open": max_open,
        "open_used": 0,
        "prefer_generated": prefer_generated,
        "seed": seed,
        "count": 0,
        "probe_queue": [],
        "current": None,
        "subtopics": {
            sid: {"level": plan["start_level"], "asked": [], "results": [], "done": False,
                  "closed_reason": "", "probed_by": None}
            for sid in order
        },
        "trace": [],
    }


def _pick_subtopic(state: dict, pending: set[str]) -> str | None:
    subs = state["subtopics"]
    while state["probe_queue"]:
        sid = state["probe_queue"][0]
        if sid in subs and not subs[sid]["done"] and sid not in pending:
            return sid
        if sid in pending and not subs[sid]["done"]:
            break  # el prerrequisito aún se genera: se sigue con otro subtema y se vuelve después
        state["probe_queue"].pop(0)
    open_subs = [sid for sid in state["order"] if not subs[sid]["done"]]
    if not open_subs:
        return None
    ready = [sid for sid in open_subs if sid not in pending]
    if not ready:
        return WAIT
    open_subs = ready
    # Intercalado: el subtema con menos preguntas; desempata el orden de prerrequisitos.
    return min(open_subs, key=lambda sid: (len(subs[sid]["asked"]), state["order"].index(sid)))


def _is_doubtful(sub_state: dict) -> bool:
    outcomes = {r["correct"] for r in sub_state["results"]}
    return len(outcomes) == 2


def _pick_question(state: dict, sid: str, bank: list[QuestionRef]) -> QuestionRef | None:
    sub = state["subtopics"][sid]
    plan = state["plan"]
    asked = set(sub["asked"])
    pool = [q for q in bank if q.subtopic_id == sid and q.id not in asked and q.level <= plan["max_level"]]
    if not pool:
        return None
    prefs = APPROACH_PREFERENCE[plan["approach"]]
    # Pregunta abierta: solo en enfoque técnico, con cupo, y cuando el subtema está
    # dudoso (aciertos y errores mezclados) o el perfil arranca en nivel avanzado.
    allow_open = state["open_used"] < state["max_open"] and plan["approach"] == "tecnico" and (
        _is_doubtful(sub) or plan["start_level"] >= 2)
    if not allow_open:
        pool = [q for q in pool if q.kind != "open"]
    if not pool:
        return None

    prefer_ia = state.get("prefer_generated", False)
    seed = state.get("seed", 0)

    def rank(q: QuestionRef) -> tuple:
        level_gap = abs(q.level - sub["level"])
        prefer_open = 0 if q.kind == "open" else 1  # a igual nivel, la abierta confirma mejor
        approach_rank = prefs.index(q.approach) if q.approach in prefs else 3
        # Estrategia de selección: 1) nivel adecuado, 2) ajuste al perfil (primero lo que la IA generó para
        # esta persona), 3) enfoque, 4) preguntas menos vistas, 5) azar por sesión (nadie ve el mismo examen).
        fit = q.fit if prefer_ia else min(q.fit, 2)
        return (level_gap, prefer_open, fit, approach_rank, q.shown // 3, jitter(seed, q.id))

    return min(pool, key=rank)


def next_question(state: dict, bank: list[QuestionRef], pending: set[str] | None = None,
                  refillable: set[str] | None = None) -> QuestionRef | str | None:
    """Elige la siguiente pregunta; None si el diagnóstico terminó; WAIT si falta que la IA termine.

    `refillable`: subtemas para los que la IA puede generar más preguntas. Si uno se queda sin
    preguntas, se espera a la IA en lugar de cerrarlo (así se cumple el número de preguntas del tema).
    """
    pending = pending or set()
    refillable = refillable or set()
    state["needs_refill"] = None
    if state["current"]:
        cur = state["current"]
        return next((q for q in bank if q.id == cur["question_id"]), None)
    while state["count"] < state["max_questions"]:
        sid = _pick_subtopic(state, pending)
        if sid is None or sid == WAIT:
            return sid
        question = _pick_question(state, sid, bank)
        if question is None:
            if sid in refillable:
                state["needs_refill"] = sid
                return WAIT
            _close(state, sid, "sin_preguntas", "No quedan preguntas disponibles en el nivel requerido.")
            continue
        state["current"] = {"question_id": question.id, "subtopic_id": sid, "level": question.level}
        return question
    return None


def _close(state: dict, sid: str, reason: str, text: str) -> None:
    sub = state["subtopics"][sid]
    if not sub["done"]:
        sub["done"] = True
        sub["closed_reason"] = reason
        state["trace"].append({"seq": state["count"], "subtopic": sid, "event": "cierre", "text": text})
    if sid in state["probe_queue"]:
        state["probe_queue"].remove(sid)


def register_answer(state: dict, question: QuestionRef, score: float, correct: bool,
                    misconception: str = "") -> dict:
    """Actualiza la escalera del subtema y devuelve la decisión tomada (para la traza)."""
    sid = question.subtopic_id
    sub = state["subtopics"][sid]
    plan = state["plan"]
    names = state["names"]
    state["count"] += 1
    state["current"] = None
    if question.kind == "open":
        state["open_used"] += 1
    sub["asked"].append(question.id)
    sub["results"].append({"question_id": question.id, "level": question.level,
                           "score": round(score, 3), "correct": correct, "misconception": misconception})

    level = question.level
    # Con pocos temas hay muchas preguntas por tema: no se cierra antes de tiempo, se sigue midiendo
    # (más conceptos del mismo tema). Con muchos temas, se cierra en cuanto hay evidencia clara.
    explore = state["per_subtopic"] > 4
    if correct:
        if level >= plan["max_level"] and not explore:
            decision = f"Acertó en el nivel máximo ({level}) de «{names[sid]}»: se cierra con dominio."
            state["trace"].append({"seq": state["count"], "subtopic": sid, "event": "acierto", "text": decision})
            _close(state, sid, "dominio", "Subtema cerrado: dominio demostrado.")
            return {"decision": decision}
        sub["level"] = min(plan["max_level"], level + 1)
        decision = (f"Acertó en nivel {level}: sube al nivel {sub['level']} en «{names[sid]}»." if level < plan["max_level"]
                    else f"Acertó en el nivel máximo ({level}) de «{names[sid]}»: sigue en ese nivel con otros conceptos.")
    else:
        detail = f" Error detectado: {misconception}." if misconception else ""
        if level > 1:
            sub["level"] = level - 1
            decision = f"Falló en nivel {level}: baja al nivel {sub['level']} para ubicar el concepto previo.{detail}"
        else:
            sub["level"] = 1
            decision = f"Falló en el nivel básico de «{names[sid]}».{detail}"
            for pre in state["prerequisites"].get(sid, []):
                pre_state = state["subtopics"].get(pre)
                if pre_state and not pre_state["done"] and pre not in state["probe_queue"]:
                    state["probe_queue"].append(pre)
                    pre_state["probed_by"] = sid
                    decision += f" Se explora el prerrequisito «{names[pre]}» para ubicar la raíz del vacío."
    state["trace"].append({"seq": state["count"], "subtopic": sid,
                           "event": "acierto" if correct else "error", "text": decision})

    results = sub["results"]
    basic_fails = sum(1 for r in results if r["level"] == 1 and not r["correct"])
    if basic_fails >= 2 and not explore:
        _close(state, sid, "vacio", "Subtema cerrado: dos errores en nivel básico confirman el vacío.")
    elif len(results) >= state["per_subtopic"]:
        _close(state, sid, "presupuesto", "Subtema cerrado: evidencia suficiente para clasificar.")
    if sid in state["probe_queue"] and len(results) >= 2:
        state["probe_queue"].remove(sid)
    return {"decision": decision}


def progress(state: dict) -> dict:
    total_subs = len(state["order"])
    done = sum(1 for s in state["subtopics"].values() if s["done"])
    expected = min(state["max_questions"], total_subs * state["per_subtopic"])
    return {"answered": state["count"], "expected": expected,
            "subtopics_done": done, "subtopics_total": total_subs}
