from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from .audit import run_audit
from .config import load_config
from .db import connect
from .doctor import checks
from .logging_config import configure_logging
from .paths import AppPaths
from .review import interactive_review
from .scanner import scan as run_scan

app = typer.Typer(help="Inspect and classify reading-media sources without modifying them.")


@app.command()
def doctor(
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML configuration path.")
    ] = None,
) -> None:
    """Report current paths and external inspection capabilities."""
    paths = AppPaths.default()
    settings = load_config(config, paths)
    configure_logging(settings.log_level)
    for check in checks(settings, paths, config):
        typer.echo(f"{check.status:7} {check.name:12} {check.detail}")


@app.command()
def scan(
    root: Annotated[Path, typer.Argument(exists=True, file_okay=False, resolve_path=True)],
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML configuration path.")
    ] = None,
    no_persist: Annotated[
        bool, typer.Option("--no-persist", help="Do not write scan state.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Recursively inspect and classify supported files under ROOT."""
    settings = load_config(config)
    configure_logging(
        settings.log_level,
        AppPaths.default().log_file if not no_persist else None,
    )
    results = run_scan(root, settings, persist=not no_persist)
    if as_json:
        typer.echo(json.dumps([asdict(item) for item in results], indent=2, default=str))
        return
    for item in results:
        classification = item.classification
        marker = "?" if classification.ambiguous else " "
        typer.echo(
            f"{marker} {classification.kind.value:7} {classification.subtype:18} "
            f"{classification.confidence:.2f}  {item.source.path}"
        )
        if item.inspection.error_message:
            typer.echo(f"    {item.inspection.status.value}: {item.inspection.error_message}")
    typer.echo(f"Scanned {len(results)} supported source(s); source files were not modified.")


@app.command("audit")
def audit_command(
    root: Annotated[Path, typer.Argument(exists=True, file_okay=False, resolve_path=True)],
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML configuration path.")
    ] = None,
    details: Annotated[
        bool, typer.Option("--details", help="Show each source's top candidate.")
    ] = False,
) -> None:
    """Match sources without accepting identities or modifying media."""
    settings = load_config(config)
    result = run_audit(root, settings)
    for key, value in result.summary.items():
        typer.echo(f"{key:40} {value}")
    for provider, activity in result.provider_activity.items():
        typer.echo(
            f"provider_activity:{provider:22} "
            f"cache_hits={activity['cache_hits']} "
            f"cache_misses={activity['cache_misses']} "
            f"network_requests={activity['network_requests']} "
            f"errors={activity['errors']} "
            f"rate_limits={activity['rate_limit_events']}"
        )
    for key, value in result.candidate_activity.items():
        typer.echo(f"candidate_activity:{key:20} {value}")
    if details:
        for item in result.items:
            top = item.scores[0] if item.scores else None
            candidate = f"{top.candidate.title} ({top.score:.1f})" if top else "unresolved"
            typer.echo(f"{item.scan.source.path.name}: {candidate}")
            if item.generation.unavailable:
                typer.echo(f"  unavailable: {'; '.join(item.generation.unavailable)}")
    typer.echo("No identity was accepted and no media file was modified.")


@app.command("review")
def review_command(
    root: Annotated[Path, typer.Argument(exists=True, file_okay=False, resolve_path=True)],
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML configuration path.")
    ] = None,
) -> None:
    """Interactively review candidates and record explicit decisions."""
    interactive_review(root, load_config(config))


@app.command()
def status(
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML configuration path.")
    ] = None,
) -> None:
    """Show persisted matching and decision counts without network access."""
    settings = load_config(config)
    if settings.database_path is None or not settings.database_path.exists():
        typer.echo("State database has not been created.")
        return
    connection = connect(settings.database_path)
    try:
        for table in ("match_runs", "match_candidates", "decisions", "provider_cache"):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            count = (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] if exists else 0
            )
            typer.echo(f"{table:20} {count}")
    finally:
        connection.close()


if __name__ == "__main__":
    app()
