"""CLI entry point: `python -m app.cli <command>`. `reembed` arrives in phase 4."""

import logging
from pathlib import Path

import typer

from app.db.connection import connect
from app.db.migrate import migrate as run_migrations
from app.pipeline.runner import finish_run, run_pipeline, start_run
from app.pipeline.stages import normalise_country_code

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


def _country_option(value: str | None) -> str | None:
    try:
        return normalise_country_code(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


@app.command()
def ingest(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True, readable=True),
    country_code: str | None = typer.Option(
        None, "--country-code", callback=_country_option,
        help="Applied to every file (else envelope source.country_code, else SG).",
    ),
) -> None:
    """Ingest every file in FOLDER (non-recursive, sorted by name) under one pipeline run."""
    files = sorted(p for p in folder.iterdir() if p.is_file())
    stats = {"processed": 0, "duplicate": 0, "failed": 0}
    with connect(autocommit=True) as conn:
        run_id = start_run(conn, "ingest")
        for path in files:
            result = run_pipeline(
                path.read_bytes(), path.name, country_code, "ingest", conn=conn, run_id=run_id
            )
            stats[result.outcome] += 1
            if result.outcome == "failed":
                typer.echo(f"failed     {path.name}: {result.error_code}")
            elif result.outcome == "duplicate":
                typer.echo(f"duplicate  {path.name}: {result.document_id}")
            else:
                typer.echo(f"processed  {path.name}: {result.document_id} ({result.status})")
        finish_run(conn, run_id, stats)

    typer.echo(f"run {run_id}")
    typer.echo(f"processed: {stats['processed']}  duplicate: {stats['duplicate']}  failed: {stats['failed']}")


if __name__ == "__main__":
    app()
