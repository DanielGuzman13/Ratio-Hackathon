"""Conexión a base de datos.

SQLite por defecto (cero instalación, ideal para la hackathon) y PostgreSQL en
producción cambiando solo DATABASE_URL. SQLAlchemy abstrae el motor.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_is_sqlite = settings.database_url.startswith("sqlite")
# timeout: la generación con IA escribe desde un hilo en segundo plano mientras la API atiende.
_connect_args = {"check_same_thread": False, "timeout": 30} if _is_sqlite else {}
# pool_pre_ping: las bases en la nube (Supabase) cierran conexiones inactivas; así se reconecta solo.
if settings.serverless and not _is_sqlite:
    # Cada instancia serverless vive poco: sin pool propio (Supabase ya tiene su pooler).
    from sqlalchemy.pool import NullPool

    engine = create_engine(settings.database_url, future=True, poolclass=NullPool)
else:
    engine = create_engine(settings.database_url, connect_args=_connect_args, future=True,
                           pool_pre_ping=not _is_sqlite, pool_recycle=300 if not _is_sqlite else -1)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


@event.listens_for(Session, "before_flush")
def _fit_strings(session, _ctx, _instances) -> None:
    """SQLite no respeta el largo de VARCHAR, PostgreSQL sí: se recorta antes de guardar
    (p. ej. una cita larga generada por la IA) para que nunca falle por longitud."""
    for obj in list(session.new) + list(session.dirty):
        table = getattr(obj, "__table__", None)
        if table is None:
            continue
        for column in table.columns:
            length = getattr(column.type, "length", None)
            value = getattr(obj, column.key, None) if length else None
            if isinstance(value, str) and len(value) > length:
                setattr(obj, column.key, value[: length - 1] + "…")


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> list[str]:
    """Crea tablas nuevas y agrega columnas nuevas a tablas existentes (migración ligera).

    Suficiente para una base de datos de demo que crece entre versiones. En producción
    se reemplaza por Alembic.
    """
    Base.metadata.create_all(engine)
    added: list[str] = []
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                default = column.default.arg if column.default is not None and not callable(column.default.arg) else None
                clause = f" DEFAULT '{default}'" if isinstance(default, str) else (
                    f" DEFAULT {default}" if isinstance(default, (int, float)) else "")
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {col_type}{clause}'))
                added.append(f"{table.name}.{column.name}")
    sync_sequences()
    return added


def sync_sequences() -> None:
    """En PostgreSQL, adelanta los contadores de id autoincremental hasta el máximo existente.

    Si se copiaron filas con su id original (migración desde SQLite), el contador queda
    atrás y el siguiente INSERT choca con una llave duplicada. Es idempotente.
    """
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            pk = list(table.primary_key.columns)
            if len(pk) != 1 or pk[0].autoincrement is not True:
                continue
            name, col = table.name, pk[0].name
            seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": name, "c": col}).scalar()
            if not seq:
                continue
            conn.execute(text(
                f'SELECT setval(:s, COALESCE((SELECT MAX("{col}") FROM {name}), 0) + 1, false)'
            ), {"s": seq})
