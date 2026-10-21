"""Modelo de datos.

Área -> Subtema -> (Fuentes, Preguntas). Cada sesión de diagnóstico guarda el
perfil, las respuestas (evidencia) y el informe con la ruta. Esa trazabilidad
es la que permite justificar cada elemento de la ruta (punto 5 del reto).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex


class Area(Base):
    __tablename__ = "areas"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(10), default="curada")  # curada | ia (tema libre)
    topic_query: Mapped[str] = mapped_column(String(200), default="")

    subtopics: Mapped[list[Subtopic]] = relationship(back_populates="area", order_by="Subtopic.order")


class Subtopic(Base):
    __tablename__ = "subtopics"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    area_id: Mapped[str] = mapped_column(ForeignKey("areas.id"))
    name: Mapped[str] = mapped_column(String(160))
    module: Mapped[str] = mapped_column(String(160), default="")
    order: Mapped[int] = mapped_column(Integer, default=0)
    prerequisites: Mapped[list] = mapped_column(JSON, default=list)
    objective_tecnico: Mapped[str] = mapped_column(Text, default="")
    objective_practico: Mapped[str] = mapped_column(Text, default="")
    concepts: Mapped[list] = mapped_column(JSON, default=list)
    risk_level: Mapped[str] = mapped_column(String(10), default="ninguno")  # ninguno | media | alta
    risk_note: Mapped[str] = mapped_column(Text, default="")

    area: Mapped[Area] = relationship(back_populates="subtopics")
    sources: Mapped[list[Source]] = relationship(back_populates="subtopic", order_by="Source.priority")
    questions: Mapped[list[Question]] = relationship(back_populates="subtopic")


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    subtopic_id: Mapped[str] = mapped_column(ForeignKey("subtopics.id"))
    kind: Mapped[str] = mapped_column(String(20))  # norma | jurisprudencia | guia
    citation: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    fragment: Mapped[str] = mapped_column(Text, default="")
    min_level: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=1)
    verified_on: Mapped[str] = mapped_column(String(20), default="")
    risk_level: Mapped[str] = mapped_column(String(10), default="ninguno")
    risk_note: Mapped[str] = mapped_column(Text, default="")

    subtopic: Mapped[Subtopic] = relationship(back_populates="sources")


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(60), primary_key=True, default=_uuid)
    subtopic_id: Mapped[str] = mapped_column(ForeignKey("subtopics.id"))
    level: Mapped[int] = mapped_column(Integer)  # 1 básico, 2 intermedio, 3 avanzado
    approach: Mapped[str] = mapped_column(String(10), default="ambos")  # tecnico | practico | ambos
    kind: Mapped[str] = mapped_column(String(10), default="mc")  # mc | open
    concept: Mapped[str] = mapped_column(String(160), default="")
    stem: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)  # [{key, text, misconception}]
    correct_key: Mapped[str] = mapped_column(String(4), default="")
    rubric: Mapped[list] = mapped_column(JSON, default=list)  # [{id, criterion, keywords}]
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    origin: Mapped[str] = mapped_column(String(10), default="seed")  # seed | ia
    # aprobada (curada o aprobada por docente) | verificada (IA + verificador, se usa en vivo)
    # | pendiente | rechazada
    status: Mapped[str] = mapped_column(String(12), default="aprobada")
    risk_level: Mapped[str] = mapped_column(String(10), default="ninguno")
    risk_note: Mapped[str] = mapped_column(Text, default="")
    verifier_note: Mapped[str] = mapped_column(Text, default="")
    # Personalización y calibración: para qué perfil/sesión se generó y cómo le ha ido en uso real.
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    profile_tag: Mapped[str] = mapped_column(String(12), default="")  # estudiante | abogado | no_abogado
    times_shown: Mapped[int] = mapped_column(Integer, default=0)
    times_correct: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    subtopic: Mapped[Subtopic] = relationship(back_populates="questions")
    source: Mapped[Source | None] = relationship()


class User(Base):
    """Usuario único por correo. role: usuario | admin."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(10), default="usuario")
    provider: Mapped[str] = mapped_column(String(10), default="correo")  # correo | google
    token_version: Mapped[int] = mapped_column(Integer, default=0)  # sube al cambiar la contraseña: cierra sesiones
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DiagnosticSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    area_id: Mapped[str] = mapped_column(ForeignKey("areas.id"))
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    approach: Mapped[str] = mapped_column(String(10), default="tecnico")
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="en_curso")  # en_curso | finalizada
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    area: Mapped[Area] = relationship()
    responses: Mapped[list[Response]] = relationship(back_populates="session", order_by="Response.seq")
    report: Mapped[Report | None] = relationship(back_populates="session", uselist=False)


class Response(Base):
    __tablename__ = "responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    question_id: Mapped[str] = mapped_column(ForeignKey("questions.id"))
    seq: Mapped[int] = mapped_column(Integer)
    answer: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float)  # 0..1
    is_correct: Mapped[bool] = mapped_column(Boolean)
    misconception: Mapped[str] = mapped_column(Text, default="")
    rubric_hits: Mapped[list] = mapped_column(JSON, default=list)
    grader: Mapped[str] = mapped_column(String(20), default="clave")  # clave | rubrica | ia
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped[DiagnosticSession] = relationship(back_populates="responses")
    question: Mapped[Question] = relationship()


class Report(Base):
    __tablename__ = "reports"

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    route: Mapped[list] = mapped_column(JSON, default=list)
    # no_requiere | pendiente | en_revision | aprobada | ajustada (aprobada con cambios)
    review_status: Mapped[str] = mapped_column(String(12), default="pendiente")
    teacher_notes: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checklist: Mapped[dict] = mapped_column(JSON, default=dict)  # {clave_marca: {checked, comment}}
    reviewed_by: Mapped[str] = mapped_column(String(120), default="")
    original_route: Mapped[list] = mapped_column(JSON, default=list)  # ruta antes de las ediciones del admin
    email_status: Mapped[str] = mapped_column(String(16), default="")  # enviando | enviado | error | sin_configurar
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped[DiagnosticSession] = relationship(back_populates="report")


class GenerationJob(Base):
    """Preparación con IA de un área para un diagnóstico (corre en segundo plano)."""

    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    area_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(String(10))  # area | tema
    topic: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(12), default="en_curso")  # en_curso | listo | error
    steps: Mapped[list] = mapped_column(JSON, default=list)  # [{key, label, status, detail, seconds}]
    pending_subtopics: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AIBatch(Base):
    """Un lote de preguntas que la IA genera durante un diagnóstico, según las respuestas dadas."""

    __tablename__ = "ai_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(12), default="en_curso")  # en_curso | listo | error
    answers_at_trigger: Mapped[int] = mapped_column(Integer, default=0)
    targets: Mapped[list] = mapped_column(JSON, default=list)  # [{subtopic_id, name, level, count, reason, focus}]
    generated: Mapped[int] = mapped_column(Integer, default=0)
    verified: Mapped[int] = mapped_column(Integer, default=0)
    seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
