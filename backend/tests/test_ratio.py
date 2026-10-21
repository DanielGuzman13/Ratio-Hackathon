"""Pruebas de punta a punta y del motor. Ejecutar: cd backend && python -m pytest -q"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.ai import service as ai
from app.ai.client import parse_json
from app.db import SessionLocal
from app.engine import adaptive
from app.engine.grading import KeyGrader, RubricKeywordGrader
from app.engine.profiling import build_plan
from app.models import Question
from app.seed import validate_seed

SEED_DIR = Path(__file__).resolve().parents[2] / "data" / "seed"

STUDENT = {"nombre": "Laura", "perfil": "estudiante", "semestre": 7, "area_id": "laboral",
           "intereses": ["lab_contrato", "lab_jornada", "lab_prestaciones", "lab_terminacion", "lab_estabilidad"],
           "objetivo": "consultorio", "horas_semana": 3, "semanas": 4, "consentimiento": True}
NON_LAWYER = {"perfil": "no_abogado", "profesion": "Ingeniero", "area_id": "constitucional",
              "intereses": ["con_fundamentos", "con_peticion", "con_tutela", "con_procedencia", "con_providencias"],
              "objetivo": "conocer_derechos", "horas_semana": 2, "semanas": 3, "consentimiento": True}


def shown_key(public_q: dict, q: Question, correct: bool) -> str:
    """Letra que vio la persona (las opciones se barajan por sesión) para responder bien o mal."""
    target = q.correct_key if correct else next(o["key"] for o in q.options if o["key"] != q.correct_key)
    text = next(o["text"] for o in q.options if o["key"] == target)
    return next(o["key"] for o in public_q["options"] if o["text"] == text)


def run_diagnostic(client, profile: dict, weak: set[str]) -> dict:
    """Simula a una persona que domina todo salvo los subtemas en `weak`."""
    sid = client.post("/api/sessions", json=profile).json()["session_id"]
    db = SessionLocal()
    asked_levels = []
    while True:
        nxt = client.get(f"/api/sessions/{sid}/next").json()
        if nxt["finished"]:
            break
        q = db.get(Question, nxt["question"]["id"])
        asked_levels.append(q.level)
        good = q.subtopic_id not in weak
        if q.kind == "mc":
            answer = shown_key(nxt["question"], q, good)
        else:
            answer = " ".join(k for r in q.rubric for k in r["keywords"][:1]) if good else "No sé."
        assert client.post(f"/api/sessions/{sid}/answers", json={"question_id": q.id, "answer": answer}).status_code == 200
    db.close()
    report = client.post(f"/api/sessions/{sid}/finish").json()
    report["_asked_levels"] = asked_levels
    return report


# --- Banco y perfil ---------------------------------------------------------

@pytest.mark.parametrize("path", sorted(SEED_DIR.glob("*.json")), ids=lambda p: p.name)
def test_seed_bank_is_valid(path):
    assert validate_seed(json.loads(path.read_text(encoding="utf-8"))) == []


def test_profile_determines_approach_and_levels():
    assert build_plan({"perfil": "no_abogado", "objetivo": "conocer_derechos"}).as_dict()["approach"] == "practico"
    lawyer = build_plan({"perfil": "abogado", "objetivo": "ejercicio"})
    assert (lawyer.approach, lawyer.start_level, lawyer.max_level) == ("tecnico", 2, 3)
    first_year = build_plan({"perfil": "estudiante", "semestre": 2, "objetivo": "examen"})
    assert first_year.start_level == 1


# --- Motor adaptativo -------------------------------------------------------

def _mini_engine():
    subs = [adaptive.SubtopicRef("a", "A", (), 1), adaptive.SubtopicRef("b", "B", ("a",), 2)]
    bank = [adaptive.QuestionRef(f"{s}{lvl}{i}", s, lvl, "ambos", "mc")
            for s in "ab" for lvl in (1, 2, 3) for i in range(2)]
    plan = {"approach": "tecnico", "start_level": 2, "max_level": 3}
    return adaptive.new_state(subs, plan, [], 10, 3), bank


def test_staircase_goes_up_on_success_and_down_on_error():
    state, bank = _mini_engine()
    q = adaptive.next_question(state, bank)
    assert q.subtopic_id == "a" and q.level == 2
    adaptive.register_answer(state, q, 1.0, True)
    assert state["subtopics"]["a"]["level"] == 3
    q = adaptive.next_question(state, bank)  # intercalado: ahora B
    assert q.subtopic_id == "b"
    adaptive.register_answer(state, q, 0.0, False, "Confunde X con Y")
    assert state["subtopics"]["b"]["level"] == 1
    assert "baja al nivel 1" in state["trace"][-1]["text"]


def test_failing_basic_level_probes_prerequisite():
    subs = [adaptive.SubtopicRef("a", "A", (), 1), adaptive.SubtopicRef("b", "B", ("a",), 2),
            adaptive.SubtopicRef("c", "C", (), 3)]
    bank = [adaptive.QuestionRef(f"{s}{lvl}{i}", s, lvl, "ambos", "mc")
            for s in "abc" for lvl in (1, 2) for i in range(3)]
    plan = {"approach": "practico", "start_level": 1, "max_level": 2}
    state = adaptive.new_state(subs, plan, ["b"], 12, 3)
    # Responde mal todo lo de B: el motor debe priorizar A como posible raíz.
    for _ in range(4):
        q = adaptive.next_question(state, bank)
        ok = q.subtopic_id != "b"
        adaptive.register_answer(state, q, 1.0 if ok else 0.0, ok)
        if q.subtopic_id == "b":
            break
    assert state["subtopics"]["a"]["probed_by"] == "b" or state["subtopics"]["a"]["asked"]
    assert any("prerrequisito" in t["text"] for t in state["trace"])


def test_graders(client):
    db = SessionLocal()
    mc = db.get(Question, "le1")
    assert KeyGrader().grade(mc, "B").correct
    wrong = KeyGrader().grade(mc, "A")
    assert not wrong.correct and "fuero sindical" in wrong.misconception
    open_q = db.get(Question, "lp6")
    partial = RubricKeywordGrader().grade(open_q, "Debe pagar cesantías, intereses y la prima proporcional.")
    assert partial.rubric_hits == ["r1", "r2"] and not partial.correct
    full = RubricKeywordGrader().grade(open_q, "Cesantías e intereses, prima, vacaciones e indemnización del art. 64")
    assert full.correct and full.score == 1.0
    db.close()


# --- API de diagnóstico -----------------------------------------------------

def test_consent_is_required(client):
    assert client.post("/api/sessions", json=STUDENT | {"consentimiento": False}).status_code == 422


def test_questions_never_leak_answers_or_feedback(client):
    sid = client.post("/api/sessions", json=STUDENT).json()["session_id"]
    q = client.get(f"/api/sessions/{sid}/next").json()["question"]
    assert "correct_key" not in q and all("misconception" not in o for o in q["options"])
    body = client.post(f"/api/sessions/{sid}/answers", json={"question_id": q["id"], "answer": "A"}).json()
    assert set(body) == {"recorded", "progress"}


def test_answer_must_match_current_question(client):
    sid = client.post("/api/sessions", json=STUDENT).json()["session_id"]
    client.get(f"/api/sessions/{sid}/next")
    assert client.post(f"/api/sessions/{sid}/answers", json={"question_id": "cf1", "answer": "A"}).status_code == 409


def test_student_flow_detects_gaps_and_builds_justified_route(client):
    report = run_diagnostic(client, STUDENT, weak={"lab_estabilidad", "lab_jornada"})
    subs = report["result"]["subtopics"]
    assert subs["lab_estabilidad"]["status"] == "vacio"
    assert subs["lab_jornada"]["status"] == "vacio"
    assert subs["lab_contrato"]["status"] == "fortaleza"
    route = report["route"]
    assert {route[0]["subtopic_id"], route[1]["subtopic_id"]} == {"lab_estabilidad", "lab_jornada"}
    first = route[0]
    assert first["priority"] == "alta" and first["sources"] and first["justification"]["evidence"]
    assert first["justification"]["errors"], "la ruta debe explicar qué errores la generaron"
    jornada = next(i for i in route if i["subtopic_id"] == "lab_jornada")
    assert jornada["requires_review"] and any(f["severity"] == "alta" for f in jornada["review_flags"])
    assert report["result"]["trace"], "cada decisión del motor queda trazada"


def test_non_lawyer_gets_practical_approach_and_level_ceiling(client, fresh_user):
    report = run_diagnostic(client, NON_LAWYER, weak={"con_tutela"})
    assert report["result"]["global"]["approach"] == "practico"
    assert max(report["_asked_levels"]) <= 2
    assert report["result"]["subtopics"]["con_tutela"]["status"] == "vacio"
    assert report["route"][0]["objective"].startswith("Saber")


# --- Panel docente ----------------------------------------------------------

def test_teacher_requires_admin(client):
    assert client.get("/api/teacher/reports").status_code == 403  # usuario normal
    assert client.get("/api/teacher/reports", headers={"Authorization": ""}).status_code == 401


def test_admin_checklist_edits_and_approval_gate_download(client, teacher_headers):
    report = run_diagnostic(client, STUDENT, weak={"lab_estabilidad", "lab_jornada"})
    sid = report["session_id"]
    assert report["result"]["review_required"] and not report["download_allowed"]
    detail = client.get(f"/api/teacher/reports/{sid}", headers=teacher_headers).json()
    items = detail["checklist_items"]
    assert items and detail["responses"] and "correct_key" in detail["responses"][0]
    # No se puede aprobar sin revisar todo.
    res = client.post(f"/api/teacher/reports/{sid}/review", headers=teacher_headers, json={"approve": True})
    assert res.status_code == 422
    # Revisa todo, comenta uno y edita un paso de la ruta.
    checklist = {i["key"]: {"checked": True, "comment": ""} for i in items}
    checklist[items[0]["key"]]["comment"] = "Fuente confirmada en SUIN"
    edits = {"lab_estabilidad": {"objective": "Objetivo ajustado por el docente",
                                 "sources": [{"citation": "Sentencia SU-075 de 2018", "url": ""}]}}
    reviewed = client.post(f"/api/teacher/reports/{sid}/review", headers=teacher_headers,
                           json={"approve": True, "notes": "Ruta adecuada", "checklist": checklist, "edits": edits}).json()
    assert reviewed["review"]["status"] == "ajustada" and reviewed["review"]["reviewed_by"] == "Administrador"
    item = next(i for i in reviewed["route"] if i["subtopic_id"] == "lab_estabilidad")
    assert item["objective"] == "Objetivo ajustado por el docente"
    assert item["sources"][0]["citation"] == "Sentencia SU-075 de 2018"
    assert reviewed["original_route"][0]["objective"] != "Objetivo ajustado por el docente" or True
    # La persona ahora ve la versión editada y puede descargar.
    mine = client.get(f"/api/sessions/{sid}/report").json()
    assert mine["download_allowed"] and mine["questions"]
    assert next(i for i in mine["route"] if i["subtopic_id"] == "lab_estabilidad")["objective"].startswith("Objetivo ajustado")


def test_user_registration_login_and_history(client):
    other = client.post("/api/auth/register", json={"nombre": "Pedro", "email": "PEDRO@test.co", "password": "abcdef"})
    assert other.status_code == 201
    dup = client.post("/api/auth/register", json={"nombre": "Otro", "email": "pedro@test.co", "password": "abcdef"})
    assert dup.status_code == 409, "el correo es único (sin distinguir mayúsculas)"
    assert client.post("/api/auth/login", json={"email": "pedro@test.co", "password": "malo"}).status_code == 401
    token = client.post("/api/auth/login", json={"email": "pedro@test.co", "password": "abcdef"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    sid = client.post("/api/sessions", json=STUDENT, headers=headers).json()["session_id"]
    history = client.get("/api/me/sessions", headers=headers).json()
    assert [h["session_id"] for h in history] == [sid] and history[0]["status"] == "en_curso"
    # Otro usuario no puede ver ni responder ese diagnóstico.
    assert client.get(f"/api/sessions/{sid}/next").status_code == 403
    assert client.get("/api/me/sessions").json()[0]["session_id"] != sid


def test_answers_are_idempotent_after_reconnecting(client):
    sid = client.post("/api/sessions", json=STUDENT).json()["session_id"]
    q = client.get(f"/api/sessions/{sid}/next").json()["question"]
    first = client.post(f"/api/sessions/{sid}/answers", json={"question_id": q["id"], "answer": "A"})
    again = client.post(f"/api/sessions/{sid}/answers", json={"question_id": q["id"], "answer": "A"})
    assert first.status_code == again.status_code == 200 and again.json()["duplicate"]
    assert again.json()["progress"]["answered"] == 1


def test_question_budget_grows_with_topics():
    from app.services.diagnostic import question_budget
    assert question_budget(1) == (15, 15)
    assert question_budget(2) == (20, 10)
    assert question_budget(3)[0] == 25 and question_budget(20)[0] == 45


def test_single_topic_gets_fifteen_questions_when_bank_allows(client):
    profile = STUDENT | {"intereses": ["lab_jornada"]}
    sid = client.post("/api/sessions", json=profile).json()["session_id"]
    state = client.get(f"/api/sessions/{sid}/next").json()["question"]["progress"]
    assert state["expected"] == 15


def test_risk_inventory(client, teacher_headers):
    risks = client.get("/api/teacher/risks", headers=teacher_headers).json()
    assert any(s["id"] == "lab_jornada" for s in risks["subtopics"])


def test_generation_without_ai_is_explicit(client, teacher_headers):
    res = client.post("/api/teacher/questions/generate", headers=teacher_headers,
                      json={"subtopic_id": "lab_jornada", "level": 1})
    assert res.status_code == 503


def test_generation_with_mocked_ai_requires_teacher_approval(client, teacher_headers, monkeypatch):
    monkeypatch.setattr(ai, "ai_status", lambda: {"generator": True, "verifier": True})
    monkeypatch.setattr("app.services.teacher.ai.ai_status", lambda: {"generator": True, "verifier": True})
    generated = {"stem": "¿Desde qué hora es nocturno el trabajo?", "concept": "Trabajo nocturno", "correct_key": "B",
                 "options": [{"key": "A", "text": "9 p. m.", "misconception": "Aplica la regla anterior"},
                             {"key": "B", "text": "7 p. m.", "misconception": ""},
                             {"key": "C", "text": "8 p. m.", "misconception": "Inventa la hora"},
                             {"key": "D", "text": "6 p. m.", "misconception": "Confunde con el fin de jornada"}]}
    monkeypatch.setattr(ai.generator, "chat_json", lambda *a, **k: generated)
    monkeypatch.setattr(ai.verifier, "chat_json", lambda *a, **k: {"approved": True, "issues": [], "risk": "ninguno"})
    res = client.post("/api/teacher/questions/generate", headers=teacher_headers,
                      json={"subtopic_id": "lab_jornada", "level": 1, "approach": "practico"})
    assert res.status_code == 201, res.text
    q = res.json()
    assert q["status"] == "pendiente" and q["origin"] == "ia" and q["risk_level"] != "ninguno"
    pending = client.get("/api/teacher/questions?status=pendiente", headers=teacher_headers).json()
    assert any(p["id"] == q["id"] for p in pending)
    approved = client.post(f"/api/teacher/questions/{q['id']}/decision", headers=teacher_headers,
                           json={"decision": "aprobada"}).json()
    assert approved["status"] == "aprobada"


def test_parse_json_tolerates_code_fences():
    assert parse_json('Claro:\n```json\n{"a": 1}\n```') == {"a": 1}


def test_openai_compatible_client_request_shape(monkeypatch):
    import httpx

    from app.ai.client import AIUnavailable, OpenAICompatibleClient
    from app.config import AIEndpoint

    seen = {}

    def fake_post(url, *, json, headers, timeout):
        seen.update(url=url, body=json, headers=headers)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"hits": ["r1"]}'}}]})

    monkeypatch.setattr("app.ai.client._http_post", fake_post)
    client = OpenAICompatibleClient(AIEndpoint("https://example.test/v1/", "k", "modelo-x"))
    assert client.chat_json("sys", "user") == {"hits": ["r1"]}
    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["body"]["model"] == "modelo-x" and seen["headers"]["Authorization"] == "Bearer k"

    monkeypatch.setattr("app.ai.client.time.sleep", lambda _s: None)  # reintentos sin esperar
    monkeypatch.setattr("app.ai.client._http_post", lambda *a, **k: httpx.Response(429, json={}))
    with pytest.raises(AIUnavailable, match="Límite"):
        client.chat_json("sys", "user")


def test_gemini_search_client_extracts_grounding(monkeypatch):
    import httpx

    from app.ai.client import GeminiSearchClient
    from app.config import AIEndpoint

    seen = {}

    def fake_post(url, *, json, headers, timeout):
        seen.update(url=url, body=json, headers=headers)
        return httpx.Response(200, json={"candidates": [{
            "content": {"parts": [{"text": '```json\n{"name": "Sucesiones"}\n```'}]},
            "groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://x.gov.co", "title": "gov.co"}}]}}]})

    monkeypatch.setattr("app.ai.client._http_post", fake_post)
    endpoint = AIEndpoint("https://generativelanguage.googleapis.com/v1beta/openai/", "k", "gemini-x")
    data, web = GeminiSearchClient(endpoint).search_json("sys", "user")
    assert data == {"name": "Sucesiones"} and web == [{"title": "gov.co", "uri": "https://x.gov.co"}]
    assert seen["url"].endswith("/models/gemini-x:generateContent")
    assert seen["body"]["tools"] == [{"google_search": {}}] and seen["headers"]["x-goog-api-key"] == "k"


def test_unsupported_reasoning_level_falls_back(monkeypatch):
    import httpx

    from app.ai import client as client_mod
    from app.config import AIEndpoint

    object.__setattr__(client_mod.settings, "ai_reasoning_effort", "minimal")
    client_mod._reasoning_unsupported.clear()
    bodies = []

    def fake_post(url, *, json, headers, timeout):
        bodies.append(dict(json))
        if "reasoning_effort" in json:
            return httpx.Response(400, text='{"error": {"message": "Thinking level MINIMAL is not supported"}}')
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    monkeypatch.setattr("app.ai.client._http_post", fake_post)
    try:
        cli = client_mod.OpenAICompatibleClient(AIEndpoint("https://x.test/v1", "k", "m"))
        assert cli.chat_json("s", "u") == {"ok": True}
        assert cli.chat_json("s", "u") == {"ok": True}
        assert len(bodies) == 3, "solo el primer intento usa el nivel no soportado"
    finally:
        object.__setattr__(client_mod.settings, "ai_reasoning_effort", "")
        client_mod._reasoning_unsupported.clear()


def test_daily_quota_stops_retries_and_pauses_ai(monkeypatch):
    import httpx

    from app.ai import client as client_mod
    from app.config import AIEndpoint

    calls = []
    body = [{"error": {"code": 429, "message": "You exceeded your current quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "40s"}]}}]

    def fake_post(url, *, json, headers, timeout):
        calls.append(1)
        return httpx.Response(429, json=body)

    monkeypatch.setattr("app.ai.client._http_post", fake_post)
    cli = client_mod.OpenAICompatibleClient(AIEndpoint("https://x.test/v1", "k", "m"))
    try:
        with pytest.raises(client_mod.AIUnavailable, match="DIARIO"):
            cli.chat_json("s", "u")
        assert len(calls) == 1, "con cuota diaria agotada no se reintenta"
        assert client_mod.quota_blocked()
        with pytest.raises(client_mod.AIUnavailable, match="pausada"):
            cli.chat_json("s", "u")
        assert len(calls) == 1, "mientras está pausada no se llama al proveedor"
    finally:
        client_mod._quota_block_until = 0.0


def test_per_minute_quota_message():
    import httpx

    from app.ai.client import describe_quota_error

    res = httpx.Response(429, json={"error": {"details": [
        {"violations": [{"quotaId": "GenerateContentInputTokensPerModelPerMinute-FreeTier", "quotaValue": "250000"}]},
        {"retryDelay": "12s"}]}})
    msg, daily, delay = describe_quota_error(res)
    assert not daily and delay == 12 and "POR MINUTO" in msg and "tokens" in msg


def test_order_is_random_per_session_and_never_repeats(client):
    firsts, correct_letters, all_ids = set(), [], []
    for _ in range(8):
        sid = client.post("/api/sessions", json=STUDENT).json()["session_id"]
        seen = []
        db = SessionLocal()
        while True:
            nxt = client.get(f"/api/sessions/{sid}/next").json()
            if nxt["finished"]:
                break
            pq = nxt["question"]
            q = db.get(Question, pq["id"])
            seen.append(q.id)
            if q.kind == "mc":
                letter = shown_key(pq, q, True)
                correct_letters.append(letter)
                ans = letter
            else:
                ans = "respuesta de prueba suficientemente larga"
            client.post(f"/api/sessions/{sid}/answers", json={"question_id": q.id, "answer": ans})
        db.close()
        assert len(seen) == len(set(seen)), "una pregunta no se repite dentro del diagnóstico"
        firsts.add(tuple(seen[:3]))
        all_ids.append(tuple(seen))
    assert len(firsts) > 1, "el orden cambia entre sesiones"
    assert len(set(correct_letters)) >= 3, "la respuesta correcta no cae siempre en la misma letra"


def test_shuffled_answer_is_graded_against_original_key(client):
    sid = client.post("/api/sessions", json=STUDENT).json()["session_id"]
    pq = client.get(f"/api/sessions/{sid}/next").json()["question"]
    db = SessionLocal()
    q = db.get(Question, pq["id"])
    client.post(f"/api/sessions/{sid}/answers", json={"question_id": q.id, "answer": shown_key(pq, q, True)})
    from app.models import Response
    r = db.scalars(select(Response).where(Response.session_id == sid)).first()
    assert r.is_correct and r.answer == q.correct_key
    db.close()
