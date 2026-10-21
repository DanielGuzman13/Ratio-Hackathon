"""Contratos de la API (validación de entrada con Pydantic)."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator


class ProfileIn(BaseModel):
    nombre: str | None = Field(default=None, max_length=120)  # se toma de la cuenta del usuario
    perfil: Literal["no_abogado", "estudiante", "abogado"]
    semestre: int | None = Field(default=None, ge=1, le=12)
    profesion: str | None = Field(default=None, max_length=80)
    area_id: str = Field(max_length=60)  # id de un área de la lista, o "libre" para un tema generado con IA
    tema_libre: str | None = Field(default=None, max_length=120)
    intereses: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=40)
    objetivo: Literal["conocer_derechos", "examen", "consultorio", "ejercicio", "actualizacion"]
    horas_semana: int = Field(ge=1, le=40)
    semanas: int = Field(ge=1, le=52)
    consentimiento: bool

    @field_validator("consentimiento")
    @classmethod
    def must_consent(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Se requiere autorización para el tratamiento de datos (Ley 1581 de 2012).")
        return value


class AnswerIn(BaseModel):
    question_id: str = Field(max_length=60)
    answer: str = Field(min_length=1, max_length=4000)


class ChecklistEntry(BaseModel):
    checked: bool = False
    comment: str = Field(default="", max_length=1000)


class SourceEdit(BaseModel):
    citation: str = Field(max_length=255)
    url: str = Field(default="", max_length=500)
    kind: str = Field(default="norma", max_length=20)
    detail: str = Field(default="", max_length=500)


class ItemEdit(BaseModel):
    objective: str | None = Field(default=None, max_length=2000)
    action: str | None = Field(default=None, max_length=60)
    focus_concepts: list[str] | None = None
    sources: list[SourceEdit] | None = None
    teacher_note: str | None = Field(default=None, max_length=2000)
    removed: bool = False


class ReviewIn(BaseModel):
    approve: bool = False
    notes: str = Field(default="", max_length=4000)
    checklist: dict[str, ChecklistEntry] = Field(default_factory=dict)
    edits: dict[str, ItemEdit] = Field(default_factory=dict)


class RegisterIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=160, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=6, max_length=128)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=160, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=1, max_length=128)


class GenerateIn(BaseModel):
    subtopic_id: str
    level: int = Field(ge=1, le=3)
    approach: Literal["tecnico", "practico", "ambos"] = "ambos"
    source_id: str | None = None


class DecisionIn(BaseModel):
    decision: Literal["aprobada", "rechazada"]
    note: str = Field(default="", max_length=1000)


class GoogleIn(BaseModel):
    credential: str = Field(min_length=20, max_length=5000)


class PasswordIn(BaseModel):
    current: str = Field(default="", max_length=128)
    new: str = Field(min_length=8, max_length=128)


class DeleteAccountIn(BaseModel):
    password: str = Field(default="", max_length=128)
    confirm: Literal["ELIMINAR"]
