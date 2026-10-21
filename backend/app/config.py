"""Configuración centralizada (patrón Settings).

Todo lo que cambia entre entornos (local, demo, producción) se lee de variables
de entorno o de un archivo .env, nunca está quemado en el código.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv es opcional
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:  # pragma: no cover
    pass

BASE_DIR = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "si", "sí"}


def _db_url(url: str) -> str:
    """Acepta la cadena tal como la da Supabase/Render (postgresql://...) y usa el driver psycopg 3."""
    url = url.strip()
    if not url:
        return f"sqlite:///{BASE_DIR / 'ratio.db'}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class AIEndpoint:
    """Un endpoint compatible con la API de OpenAI (Gemini, Groq, OpenRouter, etc.)."""

    base_url: str
    api_key: str
    model: str

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Ratio")
    app_tagline: str = os.getenv("APP_TAGLINE", "Diagnóstico jurídico adaptativo")
    database_url: str = _db_url(os.getenv("DATABASE_URL", ""))
    seed_dir: Path = BASE_DIR / "data" / "seed"
    frontend_dir: Path = BASE_DIR / "frontend"
    teacher_token: str = os.getenv("TEACHER_TOKEN", "docente-demo")  # obsoleto: ahora el admin inicia sesión
    secret_key: str = os.getenv("SECRET_KEY", "")
    admin_email: str = os.getenv("ADMIN_EMAIL", "admin@ratio.local")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "admin-demo")
    admin_name: str = os.getenv("ADMIN_NAME", "Administrador")
    token_days: int = int(os.getenv("TOKEN_DAYS", "7"))
    # Correo saliente (informes verificados). Gmail: SMTP_PASSWORD = contraseña de aplicación.
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "").replace(" ", "")
    mail_from_name: str = os.getenv("MAIL_FROM_NAME", "Ratio")
    app_url: str = os.getenv("APP_URL", "http://localhost:8000")  # enlace que va en el correo
    # Inicio de sesión con Google: ID de cliente OAuth (tipo "Aplicación web") de Google Cloud.
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    max_questions: int = int(os.getenv("MAX_QUESTIONS", "18"))
    questions_per_subtopic: int = int(os.getenv("QUESTIONS_PER_SUBTOPIC", "3"))
    # Generación en vivo: al iniciar un diagnóstico la IA genera y verifica preguntas del área.
    ai_live: bool = _bool("AI_LIVE_GENERATION", True)
    ai_free_topic: bool = _bool("AI_FREE_TOPIC", True)            # permitir "tema libre"
    ai_web_search: bool = _bool("AI_WEB_SEARCH", True)            # Google Search (solo Gemini)
    ai_prefer_generated: bool = _bool("AI_PREFER_GENERATED", True)  # usar primero preguntas de IA
    ai_questions_per_subtopic: int = int(os.getenv("AI_QUESTIONS_PER_SUBTOPIC", "6"))
    ai_max_concurrency: int = int(os.getenv("AI_MAX_CONCURRENCY", "3"))
    ai_timeout: float = float(os.getenv("AI_TIMEOUT", "90"))
    # IA adaptativa durante el diagnóstico: cada AI_BATCH_EVERY respuestas genera AI_BATCH_SIZE preguntas
    # para el perfil y los errores de la persona, hasta AI_MAX_BATCHES lotes por sesión.
    # Valores por defecto pensados para el plan gratuito de Groq (~200 mil tokens/día): unos 14 diagnósticos con IA al día.
    ai_batch_size: int = int(os.getenv("AI_BATCH_SIZE", "3"))
    ai_batch_every: int = int(os.getenv("AI_BATCH_EVERY", "3"))
    ai_batch_targets: int = int(os.getenv("AI_BATCH_TARGETS", "1"))
    ai_max_batches: int = int(os.getenv("AI_MAX_BATCHES", "4"))
    ai_force_ipv4: bool = _bool("AI_FORCE_IPV4", False)  # útil si la red tiene IPv6 roto (esperas largas)
    ai_reasoning_effort: str = os.getenv("AI_REASONING_EFFORT", "")  # ej. "none" o "low" para menos latencia
    generator: AIEndpoint = field(
        default_factory=lambda: AIEndpoint(
            base_url=os.getenv("AI_BASE_URL", ""),
            api_key=os.getenv("AI_API_KEY", ""),
            model=os.getenv("AI_MODEL_GENERATOR", ""),
        )
    )
    verifier: AIEndpoint = field(
        default_factory=lambda: AIEndpoint(
            base_url=os.getenv("AI_VERIFIER_BASE_URL", os.getenv("AI_BASE_URL", "")),
            api_key=os.getenv("AI_VERIFIER_API_KEY", os.getenv("AI_API_KEY", "")),
            model=os.getenv("AI_MODEL_VERIFIER", os.getenv("AI_MODEL_GENERATOR", "")),
        )
    )


settings = Settings()
