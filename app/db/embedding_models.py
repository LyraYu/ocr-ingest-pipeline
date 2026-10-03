import psycopg

from app.db.connection import fetch_one


def get_active(conn: psycopg.Connection) -> dict | None:
    return fetch_one(conn, "select * from embedding_models where is_active")


def upsert(conn: psycopg.Connection, model_name: str, model_version: str, dimension: int) -> None:
    conn.execute(
        """
        insert into embedding_models (model_name, model_version, dimension)
        values (%s, %s, %s)
        on conflict (model_name) do update
           set model_version = excluded.model_version, dimension = excluded.dimension
        """,
        (model_name, model_version, dimension),
    )


def activate_if_none_active(conn: psycopg.Connection, model_name: str) -> None:
    conn.execute(
        """
        update embedding_models set is_active = true
         where model_name = %s and not exists (select 1 from embedding_models where is_active)
        """,
        (model_name,),
    )


def activate(conn: psycopg.Connection, model_name: str) -> None:
    """Make `model_name` the only active model (two statements: the partial unique
    index allows at most one active row at any moment)."""
    with conn.transaction():
        conn.execute("update embedding_models set is_active = false where is_active and model_name <> %s", (model_name,))
        updated = conn.execute(
            "update embedding_models set is_active = true where model_name = %s", (model_name,)
        ).rowcount
        if updated != 1:
            raise LookupError(f"embedding model {model_name!r} is not registered")
