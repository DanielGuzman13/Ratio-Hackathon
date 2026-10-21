import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'test.db'}"  # TEST_DATABASE_URL: probar con PostgreSQL
os.environ["SECRET_KEY"] = "clave-de-pruebas"
os.environ["ADMIN_EMAIL"] = "admin@test.co"
os.environ["ADMIN_PASSWORD"] = "admin-test"
for key in ("AI_BASE_URL", "AI_API_KEY", "AI_MODEL_GENERATOR", "AI_VERIFIER_BASE_URL",
            "AI_VERIFIER_API_KEY", "AI_MODEL_VERIFIER"):
    os.environ[key] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    """Cliente con un usuario registrado: todas las solicitudes van con su sesión iniciada."""
    with TestClient(app) as c:
        res = c.post("/api/auth/register", json={"nombre": "Laura Pérez", "email": "laura@test.co",
                                                 "password": "secreto1"})
        c.headers["Authorization"] = f"Bearer {res.json()['token']}"
        yield c


@pytest.fixture(scope="session")
def admin_token(client):
    return client.post("/api/auth/login", json={"email": "admin@test.co", "password": "admin-test"}).json()["token"]


@pytest.fixture
def teacher_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Cada prueba arranca sin intentos acumulados (todas comparten la misma IP de pruebas)."""
    from app.security import limiter
    limiter.reset()
    yield


@pytest.fixture
def fresh_user(client):
    """La prueba corre con una cuenta nueva (la formación queda fija por cuenta) y luego vuelve a la de siempre."""
    import uuid
    previous = client.headers.get("Authorization")
    res = client.post("/api/auth/register", json={"nombre": "Otra Persona", "email": f"p{uuid.uuid4().hex[:10]}@test.co",
                                                  "password": "secreto12"}, headers={"Authorization": ""})
    client.headers["Authorization"] = f"Bearer {res.json()['token']}"
    yield res.json()["user"]
    client.headers["Authorization"] = previous
