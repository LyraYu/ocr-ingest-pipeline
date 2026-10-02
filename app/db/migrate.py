"""Apply numbered SQL files from migrations/ in filename order.

Each file runs in its own transaction together with its schema_migrations row,
so a failing file leaves no partial schema and is retried on the next run.
"""

import logging
from pathlib import Path

import psycopg

from app.db.connection import connect

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# Arbitrary constant: serialises concurrent `migrate` calls (api start + CLI).
_ADVISORY_LOCK_KEY = 4_211_001

_CREATE_TRACKING_TABLE = """
create table if not exists schema_migrations (
    filename   text primary key,
    applied_at timestamptz not null default now()
)
"""


def pending_migrations(conn: psycopg.Connection, migrations_dir: Path) -> list[Path]:
    applied = {row[0] for row in conn.execute("select filename from schema_migrations")}
    return [p for p in sorted(migrations_dir.glob("*.sql")) if p.name not in applied]


def migrate(migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations; return the filenames applied in this call."""
    applied_now: list[str] = []
    with connect() as conn:
        with conn.transaction():
            conn.execute("select pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
            conn.execute(_CREATE_TRACKING_TABLE)
            for path in pending_migrations(conn, migrations_dir):
                log.info("applying migration %s", path.name)
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute(
                        "insert into schema_migrations (filename) values (%s)", (path.name,)
                    )
                applied_now.append(path.name)
    return applied_now
