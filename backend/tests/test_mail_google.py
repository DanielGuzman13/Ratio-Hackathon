"""Envío del informe por correo e inicio de sesión con Google (sin red: se simulan Gmail y Google)."""
import dataclasses

from app import config
from app.api import routes
from app.services import mailer, report_pdf, users
from tests.test_ratio import STUDENT, run_diagnostic


def _settings(monkeypatch, **changes):
    new = dataclasses.replace(config.settings, **changes)
    for mod in (config, mailer, users, routes):
        monkeypatch.setattr(mod, "settings", new)
    return new


def test_report_is_emailed_with_pdf_after_admin_approval(client, teacher_headers, monkeypatch):
    _settings(monkeypatch, smtp_user="ratio@test.co", smtp_password="abcdabcdabcdabcd")
    sent = []
    monkeypatch.setattr(mailer, "_send", lambda msg: sent.append(msg))
    monkeypatch.setattr(mailer, "send_report_async", lambda sid: mailer.send_report_now(sid))  # sin hilos en la prueba

    report = run_diagnostic(client, STUDENT, weak={"lab_estabilidad"})
    sid = report["session_id"]
    assert not report["download_allowed"] and report["email"]["enabled"]
    # Mientras está en verificación no se puede pedir el envío.
    assert client.post(f"/api/sessions/{sid}/email").status_code == 409
    assert not sent

    items = client.get(f"/api/teacher/reports/{sid}", headers=teacher_headers).json()["checklist_items"]
    checklist = {i["key"]: {"checked": True, "comment": ""} for i in items}
    client.post(f"/api/teacher/reports/{sid}/review", headers=teacher_headers, json={"approve": True, "checklist": checklist})
    assert len(sent) == 1
    msg = sent[0]
    assert msg["To"] == "laura@test.co" and "ratio@test.co" in msg["From"]
    pdf = next(p for p in msg.iter_attachments() if p.get_content_type() == "application/pdf")
    assert pdf.get_payload(decode=True).startswith(b"%PDF")
    mine = client.get(f"/api/sessions/{sid}/report").json()
    assert mine["email"]["status"] == "enviado" and mine["email"]["sent_at"]
    # Reenvío inmediato: se limita para no llenar el correo.
    assert client.post(f"/api/sessions/{sid}/email").status_code == 429
    assert client.post(f"/api/teacher/reports/{sid}/email", headers=teacher_headers).status_code == 200
    assert len(sent) == 2


def test_pdf_builds_with_special_characters():
    payload = {"session_id": "x", "area_name": "Derecho laboral", "created_at": "2026-10-20T23:55:00",
               "profile": {"nombre": "Steven Ramírez", "email": "s@x.co", "perfil_label": "Estudiante", "objetivo_label": "Examen"},
               "result": {"global": {"level_name": "Básico", "approach_label": "Técnico", "mastery": 0.42},
                          "subtopics": {"a": {"name": "Estabilidad laboral reforzada", "status": "vacio", "level_name": "—",
                                              "correct": 1, "answered": 4}}},
               "route": [{"step": 1, "subtopic": "Estabilidad", "priority": "alta", "hours": 3, "objective": "Saber «x» — y…",
                          "focus_concepts": ["fuero"], "sources": [{"citation": "Sentencia SU-075 de 2018"}],
                          "justification": {"text": "Respondió 1 de 4 ≈ 25 %"}}],
               "questions": [{"seq": 1, "subtopic": "Estabilidad", "stem": "¿Qué es el fuero?", "correct": False}],
               "review": {"status": "aprobada", "reviewed_by": "Admin", "reviewed_at": "2026-10-21T00:10:00+00:00"}}
    assert report_pdf.build_pdf(payload).startswith(b"%PDF")
    assert report_pdf.local_time("2026-10-20T23:55:00") == "20/10/2026, 06:55 p. m."  # UTC -> hora de Colombia


def test_google_login_creates_account_and_rejects_bad_tokens(client, monkeypatch):
    _settings(monkeypatch, google_client_id="cliente-123.apps.googleusercontent.com")

    class Res:
        def __init__(self, data, code=200):
            self.status_code, self._data = code, data

        def json(self):
            return self._data

    good = {"aud": "cliente-123.apps.googleusercontent.com", "iss": "https://accounts.google.com",
            "email": "Steven@Gmail.com", "email_verified": "true", "name": "Steven Ramírez", "exp": "9999999999"}
    tokens = {"bueno": Res(good), "otra-app": Res(good | {"aud": "otra"}), "sin-verificar": Res(good | {"email_verified": "false"})}
    monkeypatch.setattr(users.httpx, "get", lambda url, params, timeout: tokens[params["id_token"][:20].rstrip("x")])

    res = client.post("/api/auth/google", json={"credential": "bueno".ljust(20, "x")})
    assert res.status_code == 200 and res.json()["user"]["email"] == "steven@gmail.com"
    assert res.json()["user"]["name"] == "Steven Ramírez"
    again = client.post("/api/auth/google", json={"credential": "bueno".ljust(20, "x")}).json()
    assert again["user"]["id"] == res.json()["user"]["id"], "la misma cuenta, no una nueva"
    for bad in ("otra-app", "sin-verificar"):
        assert client.post("/api/auth/google", json={"credential": bad.ljust(20, "x")}).status_code == 401
    assert client.get("/api/meta").json()["google_client_id"] == "cliente-123.apps.googleusercontent.com"
