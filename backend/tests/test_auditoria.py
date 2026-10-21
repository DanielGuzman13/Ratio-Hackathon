"""Correcciones de la auditoría: seguridad, privacidad, calidad de preguntas y motor."""
import dataclasses

from app import config
from app.ai import service
from app.engine import adaptive
from app.engine.classification import classify_global, classify_subtopic
from app.seed import wrong_weekday_dates
from app.services import users


def _new_user(client, email, password="secreto12"):
    res = client.post("/api/auth/register", json={"nombre": "Prueba", "email": email, "password": password},
                      headers={"Authorization": ""})
    assert res.status_code == 201, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_security_headers_and_api_no_store(client):
    res = client.get("/api/meta")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
    assert res.headers["cache-control"] == "no-store"
    assert "max_questions" not in res.json(), "ya no se anuncia un tope que la app no usa"
    page = client.get("/")
    assert "content-security-policy" in page.headers
    assert "onclick=" not in client.get("/app.js").text, "sin JavaScript en línea (lo bloquea la CSP)"


def test_login_is_rate_limited(client):
    for _ in range(8):
        assert client.post("/api/auth/login", json={"email": "nadie@test.co", "password": "x"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "nadie@test.co", "password": "x"}).status_code == 429


def test_input_limits(client):
    huge = "x" * 10_000
    assert client.post("/api/auth/login", json={"email": huge, "password": "x"}).status_code == 422
    assert client.post("/api/sessions", json={"perfil": "abogado", "area_id": huge, "objetivo": "examen",
                                              "horas_semana": 2, "semanas": 2, "consentimiento": True}).status_code == 422


def test_password_change_revokes_old_sessions(client):
    old = _new_user(client, "clave@test.co")
    res = client.post("/api/auth/password", headers=old, json={"current": "secreto12", "new": "nueva-clave-9"})
    assert res.status_code == 200
    assert client.get("/api/auth/me", headers=old).status_code == 401, "el token anterior ya no sirve"
    new = {"Authorization": f"Bearer {res.json()['token']}"}
    assert client.get("/api/auth/me", headers=new).status_code == 200
    assert client.post("/api/auth/password", headers=new, json={"current": "mala", "new": "otra-clave-9"}).status_code == 401


def test_export_and_delete_account(client):
    from tests.test_ratio import STUDENT
    h = _new_user(client, "borrar@test.co")
    sid = client.post("/api/sessions", headers=h, json=STUDENT | {"intereses": ["lab_jornada"]}).json()["session_id"]
    q = client.get(f"/api/sessions/{sid}/next", headers=h).json()["question"]
    client.post(f"/api/sessions/{sid}/answers", headers=h, json={"question_id": q["id"], "answer": "A"})
    data = client.get("/api/me/export", headers=h).json()
    assert data["cuenta"]["correo"] == "borrar@test.co" and data["diagnosticos"][0]["respuestas"]
    assert client.post("/api/me/delete", headers=h, json={"confirm": "ELIMINAR", "password": "mala"}).status_code == 401
    assert client.post("/api/me/delete", headers=h, json={"confirm": "ELIMINAR", "password": "secreto12"}).json()["deleted"]
    assert client.get("/api/auth/me", headers=h).status_code == 401
    assert client.post("/api/auth/login", json={"email": "borrar@test.co", "password": "secreto12"}).status_code == 401


def test_default_admin_password_blocked_in_production_and_replaced(client, monkeypatch):
    prod = dataclasses.replace(config.settings, production=True)
    monkeypatch.setattr(users, "settings", prod)
    res = client.post("/api/auth/login", json={"email": "admin@test.co", "password": "admin-test"})
    assert res.status_code == 200, "una contraseña propia funciona"
    from app.auth import hash_password
    from app.db import SessionLocal
    from app.models import User
    with SessionLocal() as db:
        admin = db.query(User).filter_by(email="admin@test.co").one()
        original_version = admin.token_version
        admin.password_hash = hash_password("admin-demo")
        db.commit()
    assert client.post("/api/auth/login", json={"email": "admin@test.co", "password": "admin-demo"}).status_code == 403
    # Al definir ADMIN_PASSWORD, el arranque la aplica sobre la de demostración.
    monkeypatch.setattr(users, "settings", dataclasses.replace(prod, admin_email="admin@test.co", admin_password="admin-test"))
    with SessionLocal() as db:
        users.ensure_admin(db)
    assert client.post("/api/auth/login", json={"email": "admin@test.co", "password": "admin-test"}).status_code == 200
    with SessionLocal() as db:  # deja vigente el token de administrador que usan las demás pruebas
        db.query(User).filter_by(email="admin@test.co").one().token_version = original_version
        db.commit()


def test_impossible_dates_and_near_duplicates_are_rejected():
    assert wrong_weekday_dates("Juan trabajó un domingo 10 de julio de 2026.")
    assert not wrong_weekday_dates("Juan trabajó el viernes 10 de julio de 2026.")
    q = {"stem": "Juan trabajó un domingo 10 de julio de 2026. ¿Qué recargo aplica según la Ley 2466 de 2025?",
         "source_id": "s1", "level": 1, "correct_key": "A",
         "options": [{"key": k, "text": t, "misconception": "" if k == "A" else "x"}
                     for k, t in zip("ABCD", ["90 %", "80 %", "100 %", "75 %"])]}
    assert any("Fecha imposible" in e for e in service._validate_generated(dict(q), {"s1"}))
    seen = {"según el artículo 239 del código sustantivo del trabajo, ¿qué se presume si despiden a una trabajadora embarazada?"}
    assert service._near_duplicate("Según el artículo 239 del Código Sustantivo del Trabajo, ¿qué se presume cuando despiden a una trabajadora embarazada?", seen)
    assert not service._near_duplicate("¿Quién puede presentar una acción de tutela?", seen)


def test_engine_avoids_repeating_a_concept():
    subs = [adaptive.SubtopicRef("s", "S", (), 0)]
    bank = [adaptive.QuestionRef("q1", "s", 1, "ambos", "mc", concept="Presunción art. 239"),
            adaptive.QuestionRef("q2", "s", 1, "ambos", "mc", concept="Presunción art. 239"),
            adaptive.QuestionRef("q3", "s", 1, "ambos", "mc", concept="Autorización del inspector")]
    state = adaptive.new_state(subs, {"approach": "practico", "start_level": 1, "max_level": 2}, ["s"],
                               max_questions=15, per_subtopic=15, seed=7)
    first = adaptive.next_question(state, bank)
    adaptive.register_answer(state, first, 0.0, False)
    second = adaptive.next_question(state, bank)
    assert second.concept != first.concept, "tras preguntar un concepto, prefiere otro antes de repetirlo"


def test_classification_needs_enough_evidence():
    two_right = [{"level": 1, "score": 1, "correct": True}, {"level": 2, "score": 1, "correct": True}]
    assert classify_subtopic(two_right)["status"] != "fortaleza", "dos aciertos no bastan para «fortaleza»"
    weak = {"s": {"status": "en_desarrollo", "level_reached": 2, "mastery": 0.39}}
    assert classify_global(weak, "tecnico", 3)["level"] < 2, "no sale «Intermedio» con 39 % de dominio"


def test_legal_corrections_are_applied_once(tmp_path):
    import json
    from app.db import SessionLocal
    from app.models import Question
    from app.seed import apply_corrections
    with SessionLocal() as db:
        q = db.query(Question).filter(Question.status == "aprobada").first()
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"preguntas": [{"id": q.id, "status": "rechazada", "nota": "Fecha imposible."}]}))
        assert apply_corrections(db, path) == 1
        assert db.get(Question, q.id).status == "rechazada"
        q.status = "aprobada"  # el administrador la corrige y la vuelve a aprobar: no se pisa
        db.commit()
        assert apply_corrections(db, path) == 0 and db.get(Question, q.id).status == "aprobada"


def test_register_rejects_html_in_email_and_hsts_in_production(client, monkeypatch):
    bad = client.post("/api/auth/register", json={"nombre": "X Y", "email": "<b>x</b>@test.co", "password": "secreto12"},
                      headers={"Authorization": ""})
    assert bad.status_code == 422
    from app import security
    monkeypatch.setattr(security, "settings", dataclasses.replace(config.settings, production=True))
    assert "strict-transport-security" in client.get("/").headers, "también en la página, no solo en la API"


def test_formation_is_saved_once_and_can_only_advance(client, fresh_user):
    from tests.test_ratio import STUDENT
    base = STUDENT | {"intereses": ["lab_jornada"]}
    assert client.get("/api/auth/me").json()["formacion"] is None, "cuenta nueva: aún no tiene formación"
    assert client.post("/api/sessions", json=base | {"perfil": "estudiante", "semestre": 4}).status_code == 201
    me = client.get("/api/auth/me").json()["formacion"]
    assert me["perfil"] == "estudiante" and me["semestre"] == 4
    # No puede cambiar a otra formación ni bajar de semestre.
    assert client.post("/api/sessions", json=base | {"perfil": "no_abogado", "semestre": None}).status_code == 422
    assert client.post("/api/sessions", json=base | {"perfil": "estudiante", "semestre": 3}).status_code == 422
    # Sí puede avanzar de semestre y graduarse.
    assert client.post("/api/sessions", json=base | {"perfil": "estudiante", "semestre": 6}).status_code == 201
    assert client.post("/api/sessions", json=base | {"perfil": "abogado", "semestre": None}).status_code == 201
    assert client.get("/api/auth/me").json()["formacion"]["perfil"] == "abogado"
    assert client.post("/api/sessions", json=base | {"perfil": "estudiante", "semestre": 9}).status_code == 422


def test_admin_email_promueve_cuenta_existente(client, monkeypatch):
    from app.db import SessionLocal
    from app.models import User
    from app.services import users
    from app.config import settings
    db = SessionLocal()
    db.add(User(email="promover@ratio.test", name="P", password_hash=users.hash_password("x" * 12),
                role="usuario", provider="google"))
    db.commit()
    monkeypatch.setattr(users, "settings", dataclasses.replace(
        settings, admin_email="promover@ratio.test", admin_password="otra-clave-segura-123"))
    users.ensure_admin(db)
    u = db.query(User).filter_by(email="promover@ratio.test").one()
    assert u.role == "admin" and users.verify_password("otra-clave-segura-123", u.password_hash)
    users.ensure_admin(db)  # idempotente
    assert db.query(User).filter_by(email="promover@ratio.test").one().role == "admin"
    db.close()


def test_solo_fuentes_dudosas_requieren_revision():
    from app.engine.routing import _flags, needs_check
    sub = {"risk_level": "ninguno"}
    ia = [{"question_id": "a" * 32, "stem": "¿Pregunta?", "origin": "ia", "tailored": True, "risk_level": "alta"}]
    ok_src = [{"citation": "Constitución, art. 86", "risk_level": "ninguno", "verified_on": "2026-01-01"}]
    flags = _flags(sub, ok_src, ia, {"confidence": "alta"})
    assert flags and not any(needs_check(f) for f in flags)  # preguntas de IA: no se revisan
    risky = [{"citation": "Ley 2466 de 2025", "risk_level": "media", "risk_note": "en transición", "verified_on": "x"}]
    assert any(needs_check(f) for f in _flags(sub, risky, [], {"confidence": "alta"}))


def test_admin_verifica_fuente_y_sobrevive_a_la_recarga(client, teacher_headers):
    from app.db import SessionLocal
    from app.seed import load_all
    risks = client.get("/api/teacher/risks", headers=teacher_headers).json()
    assert risks["sources"]
    sid = risks["sources"][0]["id"]
    r = client.post(f"/api/teacher/sources/{sid}/confirm", headers=teacher_headers, json={"confirm": True})
    assert r.status_code == 200 and r.json()["confirmed_on"]
    with SessionLocal() as db:
        load_all(db)  # lo que pasa en cada arranque del servidor
    risks = client.get("/api/teacher/risks", headers=teacher_headers).json()
    assert sid not in {s["id"] for s in risks["sources"]} and sid in {s["id"] for s in risks["confirmed"]}
    client.post(f"/api/teacher/sources/{sid}/confirm", headers=teacher_headers, json={"confirm": False})
    assert sid in {s["id"] for s in client.get("/api/teacher/risks", headers=teacher_headers).json()["sources"]}
    assert client.post(f"/api/teacher/sources/{sid}/confirm", json={"confirm": True}).status_code in (401, 403)
