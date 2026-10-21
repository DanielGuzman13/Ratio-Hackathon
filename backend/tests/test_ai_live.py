"""IA con proveedor simulado: lotes adaptativos durante el diagnóstico, precarga y tema libre."""
from __future__ import annotations

import re
import time

import pytest
from sqlalchemy import delete, select, update

from app.ai import prompts
from app.ai import service as ai
from app.db import SessionLocal
from app.models import AIBatch, Area, GenerationJob, Question, Source, Subtopic
from app.services import live_ai, preparation

BASE = {"perfil": "estudiante", "semestre": 7, "intereses": [], "objetivo": "consultorio",
        "horas_semana": 3, "semanas": 4, "consentimiento": True}


_WORDS = ("contrato salario jornada tutela despido fuero petición término recargo norma juez prueba cesantías "
          "vacaciones prima pensión sindicato huelga derecho deber plazo recurso empresa trabajador").split()


def _distinct_words() -> str:
    """Texto distinto en cada pregunta simulada (el generador descarta enunciados casi idénticos)."""
    import random
    return " ".join(random.sample(_WORDS, 12)) + f" {time.time_ns()}"


class FakeAI:
    """Simula el proveedor: respeta los niveles y el enfoque pedidos, y registra lo que recibe."""

    def __init__(self):
        self.prompts: list[str] = []

    def chat_json(self, system, user, temperature=0.3):
        if system == prompts.BATCH_GENERATOR_SYSTEM:
            self.prompts.append(user)
            ids = re.findall(r"- id: (\S+)", user)
            subtopic = re.search(r"Subtema: (.+)", user).group(1)
            levels = [int(lvl) for n, lvl in re.findall(r"(\d+) de nivel (\d)", user) for _ in range(int(n))]
            approach = (re.search(r"enfoque '(\w+)'", user) or [None, "ambos"])[1]
            qs = []
            for i, level in enumerate(levels + [levels[0]]):  # una extra que el verificador rechazará
                bad = i == len(levels)
                qs.append({"level": level, "approach": approach, "concept": f"Concepto {i}", "source_id": ids[0],
                           "stem": f"[IA]{'[malo]' if bad else ''} {subtopic} · {i} " + _distinct_words(),
                           "correct_key": "B",
                           "options": [{"key": "A", "text": f"Opción A{i}", "misconception": "Confunde A con B"},
                                       {"key": "B", "text": f"Opción B{i}", "misconception": ""},
                                       {"key": "C", "text": f"Opción C{i}", "misconception": "Cree que C aplica"},
                                       {"key": "D", "text": f"Opción D{i}", "misconception": "Ignora la regla D"}]})
            qs.append({"level": 2, "stem": "Sin fuente válida", "source_id": "inventada", "correct_key": "A",
                       "options": []})  # se descarta por formato antes de verificar
            return {"questions": qs}
        if system == prompts.BATCH_VERIFIER_SYSTEM:
            items = re.findall(r'"pregunta": "([^"]*)"', user)
            return {"results": [{"index": i, "approved": "[malo]" not in stem,
                                 "issues": ["Dos opciones correctas"] if "[malo]" in stem else [], "risk": "ninguno"}
                                for i, stem in enumerate(items)]}
        if system == prompts.TOPIC_MAP_SYSTEM:
            return TOPIC_MAP
        raise AssertionError("prompt inesperado")


TOPIC_MAP = {
    "name": "Sucesiones", "description": "Derecho sucesoral colombiano",
    "subtopics": [
        {"key": "s1", "name": "Órdenes hereditarios", "prerequisites": [], "concepts": ["Primer orden"],
         "objective_tecnico": "Aplicar los órdenes hereditarios", "objective_practico": "Saber quién hereda",
         "sources": [{"kind": "norma", "citation": "Código Civil, art. 1045", "url": "http://www.secretariasenado.gov.co/x",
                      "fragment": "Los descendientes de grado más próximo excluyen a los demás."}]},
        {"key": "s2", "name": "Sucesión intestada", "prerequisites": ["s1"], "concepts": ["Intestada"],
         "objective_tecnico": "Analizar la sucesión intestada", "objective_practico": "Qué pasa sin testamento",
         "risk_level": "alta", "risk_note": "Reforma reciente",
         "sources": [{"kind": "norma", "citation": "Código Civil, art. 1037", "url": "https://blog.example.com",
                      "fragment": "La ley regula la sucesión cuando el difunto no dispuso de sus bienes."}]},
    ],
}


@pytest.fixture
def fake_ai(client, monkeypatch):
    from app.config import settings
    overrides = {"ai_batch_every": 2, "ai_batch_targets": 2, "ai_max_batches": 8}
    previous = {key: getattr(settings, key) for key in overrides}
    for key, value in overrides.items():
        object.__setattr__(settings, key, value)  # Settings es inmutable: se ajusta solo durante la prueba
    fake = FakeAI()
    monkeypatch.setattr(ai.generator, "chat_json", fake.chat_json)
    monkeypatch.setattr(ai.verifier, "chat_json", fake.chat_json)
    monkeypatch.setattr(preparation, "ai_live_enabled", lambda: True)
    monkeypatch.setattr(live_ai, "enabled", lambda: True)
    monkeypatch.setattr(live_ai, "export_csv", lambda: None)
    yield fake
    with SessionLocal() as db:  # limpia para no afectar otras pruebas
        # Se retiran (no se borran): ya tienen respuestas que las referencian y PostgreSQL protege esas llaves.
        db.execute(update(Question).where(Question.origin == "ia").values(status="rechazada"))
        db.execute(delete(GenerationJob))
        db.execute(delete(AIBatch))
        db.commit()
    for key, value in previous.items():
        object.__setattr__(settings, key, value)


def wait_batches(sid, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with SessionLocal() as db:
            batches = db.scalars(select(AIBatch).where(AIBatch.session_id == sid)).all()
            if batches and all(b.status != "en_curso" for b in batches):
                return batches
        time.sleep(0.05)
    raise AssertionError("los lotes de IA no terminaron")


def shown_key(public_q, q, correct):
    target = q.correct_key if correct else next(o["key"] for o in q.options if o["key"] != q.correct_key)
    text = next(o["text"] for o in q.options if o["key"] == target)
    return next(o["key"] for o in public_q["options"] if o["text"] == text)


def test_only_chosen_topics_are_evaluated(client):
    chosen = ["lab_jornada", "lab_licencias", "lab_acoso"]
    sid = client.post("/api/sessions", json=BASE | {"area_id": "laboral", "intereses": chosen}).json()["session_id"]
    subtopics = set()
    with SessionLocal() as db:
        while True:
            nxt = client.get(f"/api/sessions/{sid}/next").json()
            if nxt["finished"]:
                break
            q = db.get(Question, nxt["question"]["id"])
            subtopics.add(q.subtopic_id)
            client.post(f"/api/sessions/{sid}/answers",
                        json={"question_id": q.id, "answer": shown_key(nxt["question"], q, True)})
    assert subtopics == set(chosen)
    report = client.post(f"/api/sessions/{sid}/finish").json()
    assert {i["subtopic_id"] for i in report["route"]} == set(chosen)


def test_ai_generates_tailored_batches_during_the_diagnostic(client, fake_ai, teacher_headers):
    chosen = ["lab_jornada", "lab_estabilidad"]
    res = client.post("/api/sessions", json=BASE | {"area_id": "laboral", "intereses": chosen}).json()
    sid = res["session_id"]
    assert res["preparing"] is False, "las áreas de la lista arrancan al instante"
    first = wait_batches(sid)
    assert len(first) == 1 and {t["subtopic_id"] for t in first[0].targets} <= set(chosen)
    assert "Estudiante de Derecho" in fake_ai.prompts[0] and "enfoque 'tecnico'" in fake_ai.prompts[0]
    with SessionLocal() as db:
        tailored = db.scalars(select(Question).where(Question.session_id == sid)).all()
        assert tailored and all(q.profile_tag == "estudiante" for q in tailored)
        assert all(q.level == 2 for q in tailored), "nivel inicial de un estudiante de 7.º semestre"
        assert any(q.status == "rechazada" for q in tailored), "el verificador filtra"
    # Responde mal todo lo de estabilidad: la IA debe enfocarse en esos errores.
    served_tailored = 0
    with SessionLocal() as db:
        for _ in range(8):
            nxt = client.get(f"/api/sessions/{sid}/next").json()
            if nxt["finished"]:
                break
            q = db.get(Question, nxt["question"]["id"])
            assert q.status != "rechazada"
            served_tailored += nxt["question"]["tailored"]
            ok = q.subtopic_id != "lab_estabilidad"
            client.post(f"/api/sessions/{sid}/answers",
                        json={"question_id": q.id, "answer": shown_key(nxt["question"], q, ok)})
            wait_batches(sid)
    batches = wait_batches(sid)
    assert len(batches) >= 3, "cada pocas respuestas se genera un nuevo lote"
    assert served_tailored >= 1, "el motor usa las preguntas generadas para esta persona"
    focused = [t for b in batches for t in b.targets if t["focus"]]
    assert focused and focused[0]["subtopic_id"] == "lab_estabilidad"
    assert any("Errores que la persona ya mostró" in p for p in fake_ai.prompts)
    # El docente ve qué hizo la IA durante el diagnóstico.
    client.post(f"/api/sessions/{sid}/finish")
    detail = client.get(f"/api/teacher/reports/{sid}", headers=teacher_headers).json()
    assert detail["ai_batches"] and detail["ai_batches"][0]["targets"]


def test_non_lawyer_gets_practical_ai_questions(client, fake_ai):
    profile = BASE | {"perfil": "no_abogado", "semestre": None, "objetivo": "conocer_derechos",
                      "area_id": "constitucional", "intereses": ["con_peticion", "con_tutela"]}
    sid = client.post("/api/sessions", json=profile).json()["session_id"]
    wait_batches(sid)
    assert "enfoque 'practico'" in fake_ai.prompts[0] and "sin formación jurídica" in fake_ai.prompts[0]
    with SessionLocal() as db:
        tailored = db.scalars(select(Question).where(Question.session_id == sid, Question.status == "verificada")).all()
        assert tailored and all(q.level == 1 and q.approach == "practico" for q in tailored)


def test_pregenerar_fills_the_bank(client, fake_ai):
    with SessionLocal() as db:
        area = db.get(Area, "constitucional")
        job = preparation.create_job(db, kind="area", area=area)
        needing, job_id = list(job.pending_subtopics), job.id
    preparation._generate_all(job_id, needing, "media")
    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        assert all(s["status"] == "listo" for s in job.steps)
        assert job.stats["verificadas"] >= len(needing) * 4
        assert job.stats["rechazadas"] == len(needing) and job.stats["descartadas"] == len(needing)


def test_free_topic_builds_area_with_ai(client, fake_ai):
    res = client.post("/api/sessions", json=BASE | {"area_id": "libre", "tema_libre": "Sucesiones"}).json()
    assert res["preparing"] is True and res["progress"] is None
    sid = res["session_id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        status = client.get(f"/api/sessions/{sid}/preparation").json()
        if status["job"]["status"] != "en_curso":
            break
        time.sleep(0.05)
    assert status["job"]["status"] == "listo", status["job"]
    assert status["ready_to_start"] and status["area"] == "Sucesiones"
    with SessionLocal() as db:
        area = db.get(Area, "ia-sucesiones")
        assert area.origin == "ia" and len(area.subtopics) == 2
        second = area.subtopics[1]
        assert second.prerequisites == [area.subtopics[0].id] and second.risk_level == "alta"
        blog = db.scalars(select(Source).where(Source.subtopic_id == second.id)).first()
        assert blog.risk_level == "alta", "una fuente fuera de dominios oficiales queda en riesgo alto"
    nxt = client.get(f"/api/sessions/{sid}/next").json()
    assert nxt["question"]["subtopic"] == "Órdenes hereditarios"
    assert all(a["id"] != "ia-sucesiones" for a in client.get("/api/areas").json())
    again = client.post("/api/sessions", json=BASE | {"area_id": "libre", "tema_libre": "sucesiones"}).json()
    assert again["preparing"] is False


def test_free_topic_requires_ai(client):
    res = client.post("/api/sessions", json=BASE | {"area_id": "libre", "tema_libre": "Familia"})
    assert res.status_code == 503


def test_engine_waits_for_pending_subtopics():
    from app.engine import adaptive

    subs = [adaptive.SubtopicRef("a", "A", (), 1), adaptive.SubtopicRef("b", "B", (), 2)]
    bank = [adaptive.QuestionRef(f"a{i}", "a", 1, "ambos", "mc") for i in range(3)]
    state = adaptive.new_state(subs, {"approach": "practico", "start_level": 1, "max_level": 2}, [], 10, 2)
    assert adaptive.next_question(state, bank, pending={"a", "b"}) == adaptive.WAIT
    q = adaptive.next_question(state, bank, pending={"b"})
    assert q.subtopic_id == "a"


def test_engine_prefers_questions_tailored_to_the_person():
    from app.engine import adaptive

    subs = [adaptive.SubtopicRef("a", "A", (), 1)]
    bank = [adaptive.QuestionRef("curada", "a", 1, "ambos", "mc", "seed", fit=2),
            adaptive.QuestionRef("otro_perfil", "a", 1, "ambos", "mc", "ia", fit=3),
            adaptive.QuestionRef("para_ti", "a", 1, "ambos", "mc", "ia", fit=0)]
    state = adaptive.new_state(subs, {"approach": "practico", "start_level": 1, "max_level": 2}, [], 5, 3,
                               prefer_generated=True)
    assert adaptive.next_question(state, bank).id == "para_ti"


def test_single_topic_reaches_fifteen_questions_with_ai_refill(client, fake_ai):
    """Un tema con pocas preguntas en el banco: el motor espera a la IA en lugar de terminar antes."""
    sid = client.post("/api/sessions", json=BASE | {"area_id": "laboral", "intereses": ["lab_licencias"]}).json()["session_id"]
    answered, waits = 0, 0
    with SessionLocal() as db:
        for _ in range(200):
            nxt = client.get(f"/api/sessions/{sid}/next").json()
            if nxt["finished"]:
                break
            if nxt.get("waiting"):
                waits += 1
                time.sleep(0.05)
                continue
            q = db.get(Question, nxt["question"]["id"])
            assert q.subtopic_id == "lab_licencias"
            client.post(f"/api/sessions/{sid}/answers",
                        json={"question_id": q.id, "answer": shown_key(nxt["question"], q, answered % 2 == 0)})
            answered += 1
    assert answered == 15, answered  # el banco tenía solo 3 preguntas curadas de este tema


def test_malformed_model_output_does_not_break_batch(monkeypatch):
    """El modelo a veces devuelve opciones como texto o preguntas como cadenas: se normaliza o se descarta sin romper el lote."""
    from app.ai import service
    src = [{"id": "s1", "citation": "Constitución Política, art. 86", "fragment": "Toda persona tendrá acción de tutela..."}]
    payload = {"questions": [
        "esto no es una pregunta",
        {"stem": "Según el artículo 86 de la Constitución Política, ¿quién puede interponer tutela?", "level": 1,
         "source_id": "s1", "correct_key": "A",
         "options": ["A) Toda persona", "B) Solo abogados", "C) Solo el Estado", "D) Solo mayores de edad"]},
        {"stem": "Según la Constitución Política, ¿ante quién se presenta la tutela?", "level": 1, "source_id": "s1",
         "correct_key": "B", "options": {"A": {"text": "Notario", "misconception": "x"}, "B": {"text": "Juez"},
                                         "C": {"text": "Alcalde", "misconception": "y"}, "D": {"text": "Congreso", "misconception": "z"}}},
    ]}
    monkeypatch.setattr(service.generator, "chat_json", lambda *a, **k: payload)
    good, errors = service.generate_batch(subtopic_name="Tutela", concepts=["tutela"], sources=src, count=3,
                                          existing_stems=[], levels=[1, 1, 1])
    assert len(good) == 1 and good[0]["options"][1] == {"key": "B", "text": "Juez"}
    assert any("formato inválido" in e for e in errors)
    assert any("no declara el error" in e for e in errors)  # opciones en texto sin errores declarados: se descarta
    monkeypatch.setattr(service.verifier, "chat_json", lambda *a, **k: ["no es un dict"])
    assert service.verify_batch(good, {"s1": src[0]})[0]["approved"] is False


def test_serverless_mode_runs_ai_in_its_own_request(client, fake_ai):
    """En Vercel no hay hilos: el lote queda «pendiente» y lo ejecuta la solicitud /ai/run del navegador."""
    from app.config import settings
    object.__setattr__(settings, "serverless", True)
    try:
        res = client.post("/api/sessions", json=BASE | {"area_id": "laboral", "intereses": ["lab_jornada"]}).json()
        sid = res["session_id"]
        with SessionLocal() as db:
            batches = db.scalars(select(AIBatch).where(AIBatch.session_id == sid)).all()
            assert [b.status for b in batches] == ["pendiente"], "no se lanzó ningún hilo"
        nxt = client.get(f"/api/sessions/{sid}/next").json()
        assert nxt["ai"]["pending"] is True, "el navegador sabe que debe pedir la generación"
        ran = client.post(f"/api/sessions/{sid}/ai/run").json()["ran"]
        assert len(ran) == 1
        assert client.post(f"/api/sessions/{sid}/ai/run").json()["ran"] == [], "no se repite el mismo lote"
        with SessionLocal() as db:
            assert db.get(AIBatch, ran[0]).status == "listo"
            assert db.scalars(select(Question).where(Question.session_id == sid)).all()
        assert client.get(f"/api/sessions/{sid}/next").json()["ai"]["pending"] is False
    finally:
        object.__setattr__(settings, "serverless", False)
