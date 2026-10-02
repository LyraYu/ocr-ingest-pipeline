"""CLI entry point: `python -m app.cli <command>`.

Phase 1 provides `migrate`; `ingest` and `reembed` arrive in later phases.
"""

import logging

import typer

from app.db.migrate import migrate as run_migrations

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Document ingestion pipeline."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@app.command()
def migrate() -> None:
    """Apply pending SQL migrations from migrations/."""
    applied = run_migrations()
    if applied:
        typer.echo(f"applied: {', '.join(applied)}")
    else:
        typer.echo("no pending migrations")


if __name__ == "__main__":
    app()
