"""Copia los datos de una base a otra (p. ej. del ratio.db local a Supabase/PostgreSQL).

Uso, desde la carpeta backend, con DATABASE_URL del .env apuntando a la base NUEVA:

    python -m app.migrar                      # copia desde ../ratio.db
    python -m app.migrar --desde sqlite:///C:/ruta/ratio.db

Es seguro repetirlo: lo que ya existe en la base nueva (misma llave) no se duplica.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, select

from .config import BASE_DIR, _db_url
from .db import Base, engine, init_db
from . import models  # noqa: F401  (registra las tablas)


def _fit(table, row: dict) -> dict:
    out = {}
    for column in table.columns:
        if column.name not in row:
            continue
        value = row[column.name]
        length = getattr(column.type, "length", None)
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)  # SQLite guarda UTC sin zona: se marca como UTC
        if length and isinstance(value, str) and len(value) > length:
            value = value[: length - 1] + "…"
        out[column.name] = value
    return out


def migrate(source_url: str) -> dict[str, tuple[int, int]]:
    source = create_engine(_db_url(source_url))
    if str(source.url) == str(engine.url):
        raise SystemExit("La base de origen y la de destino son la misma: revisa DATABASE_URL en el .env.")
    init_db()  # crea las tablas en la base nueva
    source_tables = set(inspect(source).get_table_names())
    report = {}
    with source.connect() as src, engine.begin() as dst:
        for table in Base.metadata.sorted_tables:  # orden que respeta las llaves foráneas
            if table.name not in source_tables:
                continue
            src_cols = {c["name"] for c in inspect(source).get_columns(table.name)}
            cols = [c for c in table.columns if c.name in src_cols]
            pk = [c.name for c in table.primary_key.columns]
            existing = {tuple(r) for r in dst.execute(select(*[table.c[k] for k in pk]))}
            emails = ({r[0] for r in dst.execute(select(table.c.email))} if table.name == "users" else set())
            rows, skipped = [], 0
            for r in src.execute(select(*[table.c[c.name] for c in cols])).mappings():
                key = tuple(r[k] for k in pk)
                if key in existing or (emails and r["email"] in emails):
                    skipped += 1
                    continue
                rows.append(_fit(table, dict(r)))
            if rows:
                dst.execute(table.insert(), rows)
            report[table.name] = (len(rows), skipped)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--desde", default=f"sqlite:///{BASE_DIR / 'ratio.db'}", help="base de origen")
    args = parser.parse_args()
    print(f"Destino: {engine.url.render_as_string(hide_password=True)}")
    for name, (copied, skipped) in migrate(args.desde).items():
        print(f"  {name:<18} {copied:>5} copiadas · {skipped} ya existían")
    print("Listo. Arranca el servidor normalmente.")


if __name__ == "__main__":
    main()
