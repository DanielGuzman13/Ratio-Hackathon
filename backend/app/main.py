"""Punto de entrada de la aplicación.

uvicorn app.main:app --reload   (desde la carpeta backend/)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routes import docente, public
from .config import settings
from .db import SessionLocal, init_db
from .security import security_headers
from .seed import apply_corrections, load_all
from .services.users import ensure_admin


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()  # crea tablas y agrega columnas nuevas si la base de datos es de una versión anterior
    with SessionLocal() as db:
        load_all(db)  # idempotente: sincroniza el banco semilla en cada arranque
        apply_corrections(db)  # correcciones jurídicas puntuales (data/review/correcciones.json)
        ensure_admin(db)  # administrador inicial (ADMIN_EMAIL / ADMIN_PASSWORD)
    yield


# En producción no se publica la documentación interactiva de la API (/docs, /redoc, /openapi.json).
_docs = {} if not settings.production else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title=f"{settings.app_name} API", description=settings.app_tagline, version="1.0.0",
              lifespan=lifespan, **_docs)
app.middleware("http")(security_headers)
# La app web se sirve desde el mismo origen: CORS solo para orígenes declarados (CORS_ORIGINS).
_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type"])
app.include_router(public)
app.include_router(docente)

if settings.frontend_dir.exists():
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
