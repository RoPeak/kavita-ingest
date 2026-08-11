from __future__ import annotations

import json
import sqlite3
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
from .plan_store import PlanStore
from .planning import validate_plan_payload
from .providers.models import NormalizedCandidate, ProviderName, RecordType
from .review import interactive_review
from .run_groups import RunGroupRepository
from .scanner import scan as run_scan

app = typer.Typer(help="Inspect and classify reading-media sources without modifying them.")
plan_app = typer.Typer(help="Create and approve immutable offline execution plans.")
run_group_app = typer.Typer(help="Inspect and manage explicit comic run-group choices.")
app.add_typer(plan_app, name="plan")
app.add_typer(run_group_app, name="run-group")


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
        for table in (
            "match_runs",
            "match_candidates",
            "decisions",
            "run_group_decisions",
            "plans",
            "provider_cache",
        ):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            count = (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] if exists else 0
            )
            typer.echo(f"{table:20} {count}")
    finally:
        connection.close()


def _state_connection(config: Path | None) -> sqlite3.Connection:
    settings = load_config(config)
    if settings.database_path is None:
        raise typer.BadParameter("database path is required")
    from .db import migrate

    migrate(settings.database_path)
    return connect(settings.database_path)


def _plan_store(config: Path | None) -> tuple[sqlite3.Connection, PlanStore]:
    connection = _state_connection(config)
    return connection, PlanStore(connection)


@plan_app.command("create")
def plan_create(
    resolved_plan: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Canonicalize and store a fully resolved plan document as a new draft."""
    raw = json.loads(resolved_plan.read_text(encoding="utf-8"))
    payload = json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    validate_plan_payload(payload)
    connection, store = _plan_store(config)
    try:
        plan = store.import_bytes(payload)
        typer.echo(f"Created draft plan {plan.id} sha256={plan.sha256} bytes={plan.byte_length}")
    finally:
        connection.close()


@plan_app.command("show")
def plan_show(
    plan_id: int,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Show authoritative plan bytes and approval state without network access."""
    connection, store = _plan_store(config)
    try:
        plan = store.get(plan_id)
        typer.echo(
            f"plan={plan.id} status={plan.status} sha256={plan.sha256} bytes={plan.byte_length}"
        )
        typer.echo(plan.canonical_json.decode("utf-8"))
    finally:
        connection.close()


@plan_app.command("approve")
def plan_approve(
    plan_id: int,
    digest: Annotated[str, typer.Option("--digest", help="Exact displayed SHA-256 digest.")],
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Explicitly bind approval to the exact authoritative plan digest."""
    connection, store = _plan_store(config)
    try:
        plan = store.approve(plan_id, digest)
        typer.echo(f"Approved plan {plan.id} sha256={plan.sha256}")
    finally:
        connection.close()


@plan_app.command("export")
def plan_export(
    plan_id: int,
    destination: Path,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Export the exact canonical bytes stored in SQLite."""
    connection, store = _plan_store(config)
    try:
        store.export(plan_id, destination)
        typer.echo(f"Exported plan {plan_id} to {destination}")
    finally:
        connection.close()


@plan_app.command("import")
def plan_import(
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Import canonical plan bytes as an unapproved draft after digest validation."""
    connection, store = _plan_store(config)
    try:
        plan = store.import_bytes(source.read_bytes())
        typer.echo(f"Imported draft plan {plan.id} sha256={plan.sha256}")
    finally:
        connection.close()


@run_group_app.command("choose")
def run_group_choose(
    group_key: str,
    provider_run_id: str,
    snapshot: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    manual: Annotated[bool, typer.Option(help="Mark this as a manual override.")] = False,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Explicitly select or supersede a provider run for a local comic group."""
    value = json.loads(snapshot.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise typer.BadParameter("run snapshot must be a JSON object")
    try:
        run = NormalizedCandidate.from_dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise typer.BadParameter(f"invalid normalized run snapshot: {exc}") from exc
    if (
        run.provider is not ProviderName.COMIC_VINE
        or run.record_type is not RecordType.COMIC_RUN
        or run.provider_id != provider_run_id
    ):
        raise typer.BadParameter("snapshot must describe the selected Comic Vine run id")
    connection = _state_connection(config)
    try:
        decision = RunGroupRepository(connection).choose(
            group_key, "comic_vine", provider_run_id, value, manual=manual
        )
        typer.echo(f"Recorded run-group decision {decision.id}; no issue identity was accepted.")
    finally:
        connection.close()


@run_group_app.command("clear")
def run_group_clear(
    group_key: str,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Append a clearing decision without deleting run-group history."""
    connection = _state_connection(config)
    try:
        decision = RunGroupRepository(connection).clear(group_key, "comic_vine")
        typer.echo(f"Cleared run-group choice with decision {decision.id}.")
    finally:
        connection.close()


@run_group_app.command("history")
def run_group_history(
    group_key: str,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Display the append-only decision trail without querying providers."""
    connection = _state_connection(config)
    try:
        history = RunGroupRepository(connection).history(group_key, "comic_vine")
        for decision in history:
            typer.echo(
                f"{decision.id} {decision.created_at} {decision.decision_type.value} "
                f"run={decision.provider_run_id or '-'} supersedes={decision.supersedes_id or '-'}"
            )
    finally:
        connection.close()


if __name__ == "__main__":
    app()
