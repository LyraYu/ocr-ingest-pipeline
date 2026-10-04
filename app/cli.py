"""CLI entry point: `python -m app.cli <command>`."""

from pathlib import Path

import typer

from app.db import embedding_models
from app.db.connection import connect, fetch_all
from app.db.migrate import migrate as run_migrations
from app.embedding import get_embedder
from app.ocr import InvalidCountryCodeError, normalise_country_code
from app.pipeline.runner import describe_document, finish_run, run_pipeline, start_run
from app.pipeline.stages import PRE_EMBED_STAGES, StageFailed, embed_documents, stage_chunk
from app.security import pii

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Document ingestion pipeline."""
    pii.install()  # PII masking on every log handler (docs/DESIGN.md §8)


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
    except InvalidCountryCodeError as exc:
        raise typer.BadParameter(str(exc)) from None


@app.command()
def ingest(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True, readable=True),
    country_code: str | None = typer.Option(
        None, "--country-code", callback=_country_option,
        help="Applied to every file (else envelope source.country_code, else SG).",
    ),
) -> None:
    """Ingest every file in FOLDER (non-recursive, sorted by name) under one pipeline run.

    Each file runs through receive → chunk; then all new chunks of the batch are
    embedded in one encode call. Pending migrations are applied first, so this works
    on a fresh database (e.g. right after `docker compose up -d`)."""
    run_migrations()
    files = sorted(p for p in folder.iterdir() if p.is_file())
    stats = {"processed": 0, "duplicate": 0, "failed": 0}
    with connect(autocommit=True) as conn:
        run_id = start_run(conn, "ingest")
        results = {
            path.name: run_pipeline(
                path.read_bytes(), path.name, country_code, "ingest",
                conn=conn, run_id=run_id, stages=PRE_EMBED_STAGES,
            )
            for path in files
        }
        to_embed = [r.document_id for r in results.values() if r.outcome == "processed"]
        embed_documents(conn, to_embed, run_id)
        for name, result in results.items():
            if result.outcome == "processed":
                result = describe_document(conn, result.document_id, duplicate=False)
            stats[result.outcome] += 1
            if result.outcome == "failed":
                typer.echo(f"failed     {name}: {result.error_code}")
            elif result.outcome == "duplicate":
                typer.echo(f"duplicate  {name}: {result.document_id}")
            else:
                typer.echo(f"processed  {name}: {result.document_id} ({result.document_type}, {result.chunks} chunks)")
        finish_run(conn, run_id, stats)

    typer.echo(f"run {run_id}")
    typer.echo(f"processed: {stats['processed']}  duplicate: {stats['duplicate']}  failed: {stats['failed']}")


@app.command()
def reembed(
    model: str = typer.Option(..., "--model", help="sentence-transformers model, e.g. BAAI/bge-small-en-v1.5"),
) -> None:
    """Embed every embedded document's chunks with MODEL, then make MODEL the active
    (searched) model. Existing embeddings of other models are kept; the switch only
    happens if every document succeeded."""
    try:
        embedder = get_embedder(model)
    except Exception as exc:
        typer.echo(f"cannot load model {model}: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from None

    with connect(autocommit=True) as conn:
        previous = embedding_models.get_active(conn)
        run_id = start_run(conn, "reembed", embedding_model=model)
        conn.execute(
            "update pipeline_runs set embedding_model_version = %s where id = %s",
            (embedder.model_version, run_id),
        )
        doc_ids = [r["id"] for r in fetch_all(conn, "select id from documents where status = 'embedded' order by ingested_at")]
        failed: dict = {}
        chunked = []
        for doc_id in doc_ids:
            try:
                stage_chunk(conn, doc_id, run_id, fail_document=False)  # reuses existing chunks
                chunked.append(doc_id)
            except StageFailed as exc:
                failed[doc_id] = exc.error_code
        for doc_id, error in embed_documents(conn, chunked, run_id, model, fail_document=False).items():
            if error:
                failed[doc_id] = error

        embedding_models.upsert(conn, embedder.model_name, embedder.model_version, embedder.dimension)
        stats = {"processed": len(doc_ids) - len(failed), "duplicate": 0, "failed": len(failed)}
        if not failed:
            embedding_models.activate(conn, embedder.model_name)
        finish_run(conn, run_id, stats)
        chunk_count = fetch_all(
            conn,
            "select count(*) as n from chunk_embeddings where model_name = %s and model_version = %s",
            (embedder.model_name, embedder.model_version),
        )[0]["n"]

    for doc_id, error in failed.items():
        typer.echo(f"failed     {doc_id}: {error}")
    typer.echo(f"run {run_id}")
    typer.echo(f"documents: {len(doc_ids)}  re-embedded: {stats['processed']}  failed: {len(failed)}")
    typer.echo(f"{embedder.model_name} ({embedder.model_version}): {chunk_count} chunk embeddings")
    if failed:
        typer.echo(f"active model unchanged: {previous['model_name'] if previous else None}")
        raise typer.Exit(1)
    typer.echo(f"active model: {embedder.model_name} (was {previous['model_name'] if previous else None})")


if __name__ == "__main__":
    app()
