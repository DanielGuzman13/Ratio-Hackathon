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
from .seed import load_all
from .services.users import ensure_admin


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()  # crea tablas y agrega columnas nuevas si la base de datos es de una versión anterior
    with SessionLocal() as db:
        load_all(db)  # idempotente: sincroniza el banco semilla en cada arranque
        ensure_admin(db)  # administrador inicial (ADMIN_EMAIL / ADMIN_PASSWORD)
    yield


app = FastAPI(title=f"{settings.app_name} API", description=settings.app_tagline, version="1.0.0",
              lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(public)
app.include_router(docente)

if settings.frontend_dir.exists():
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
