from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .apply_engine import ApplyEngine, ApplyPreview, ApplyRefused, ApplySummary
from .audit import run_audit
from .config import load_config, write_initial_config
from .db import connect
from .doctor import checks
from .locking import LockUnavailable
from .logging_config import configure_logging, provider_secrets, set_console_verbosity
from .matching import CandidateScore, usable_identity_scores
from .paths import AppPaths
from .plan_store import PlanStore
from .planning_service import PlanBuilder
from .providers.models import NormalizedCandidate, ProviderName, RecordType
from .review import interactive_review
from .rollback import preview_rollback
from .run_groups import RunGroupRepository
from .scanner import scan as run_scan

app = typer.Typer(
    help="Inspect, plan, and safely execute explicitly approved reading-media ingestion."
)
plan_app = typer.Typer(help="Create and approve immutable offline execution plans.")
run_group_app = typer.Typer(help="Inspect and manage explicit comic run-group choices.")
app.add_typer(plan_app, name="plan")
app.add_typer(run_group_app, name="run-group")

OUTPUT_VERSION = "1"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"kavita-ingest {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    context: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the application version and exit.",
        ),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Show informational operational logs on stderr.")
    ] = False,
    debug: Annotated[
        bool, typer.Option("--debug", help="Show debug operational logs on stderr.")
    ] = False,
) -> None:
    """Operate a reviewed, explicitly approved Kavita ingestion workflow."""
    del version
    set_console_verbosity(verbose=verbose, debug=debug)
    if context.invoked_subcommand is None:
        _workflow_menu()


@app.command("init")
def init_command(
    config: Annotated[
        Path | None, typer.Option("--config", help="Configuration path to create.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Explicitly replace an existing configuration.")
    ] = False,
) -> None:
    """Create a commented, secret-free initial TOML configuration."""
    destination = config or AppPaths.default().config_file
    try:
        write_initial_config(destination, force=force)
    except FileExistsError as exc:
        typer.echo(f"REFUSED: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Created configuration: {destination}")
    typer.echo("No API keys were written. Next: kavita-ingest doctor")


@app.command()
def doctor(
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML configuration path.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit versioned JSON.")] = False,
) -> None:
    """Run the authoritative local environment and capability preflight."""
    paths = AppPaths.default()
    settings = load_config(config, paths)
    configure_logging(settings.log_level, secrets=provider_secrets(settings))
    results = checks(settings, paths, config)
    if as_json:
        _emit_json("doctor", {"checks": [check.to_dict() for check in results]})
        return
    for category in dict.fromkeys(check.category for check in results):
        typer.echo(f"\n{category.title()}")
        for check in (item for item in results if item.category == category):
            typer.echo(f"{check.status:7} {check.name:22} {check.detail}")


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
        _log_file(settings) if not no_persist else None,
        secrets=provider_secrets(settings),
    )
    results = run_scan(root, settings, persist=not no_persist)
    if as_json:
        _emit_json("scan", {"count": len(results), "items": [asdict(item) for item in results]})
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
    metrics: Annotated[
        bool, typer.Option("--metrics", help="Show provider and matching diagnostic counters.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit versioned JSON.")] = False,
) -> None:
    """Match sources without accepting identities or modifying media."""
    settings = load_config(config)
    configure_logging(
        settings.log_level,
        _log_file(settings),
        secrets=provider_secrets(settings),
    )
    result = run_audit(root, settings)
    if as_json:
        _emit_json(
            "audit",
            {
                "run_id": result.run_id,
                "summary": result.summary,
                "provider_activity": result.provider_activity,
                "candidate_activity": result.candidate_activity,
            },
        )
        return
    provider_problems = (
        result.summary["provider_unavailable"] + result.summary["partial_provider_unavailable"]
    )
    typer.echo(f"Scanned:            {result.summary['sources']}")
    typer.echo(f"Strong matches:     {result.summary['eligible_high_confidence']}")
    typer.echo(f"Needs review:       {result.summary['review_required']}")
    typer.echo(f"Unresolved:         {result.summary['unresolved']}")
    typer.echo(f"Provider problems:  {provider_problems}")
    if metrics:
        for key, value in result.summary.items():
            typer.echo(f"summary:{key:29} {value}")
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
            useful = usable_identity_scores(item.scores)
            top = useful[0] if useful else None
            candidate = _candidate_detail(top) if top else "unresolved"
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
    settings = load_config(config)
    configure_logging(
        settings.log_level,
        _log_file(settings),
        secrets=provider_secrets(settings),
    )
    interactive_review(root, settings)


@app.command()
def status(
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML configuration path.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit versioned JSON.")] = False,
) -> None:
    """Show persisted matching and decision counts without network access."""
    settings = load_config(config)
    if settings.database_path is None or not settings.database_path.exists():
        if as_json:
            _emit_json("status", {"database_exists": False, "counts": {}})
            return
        typer.echo("State database has not been created.")
        return
    connection = connect(settings.database_path)
    try:
        counts: dict[str, int] = {}
        for table in (
            "match_runs",
            "match_candidates",
            "decisions",
            "run_group_decisions",
            "plans",
            "apply_runs",
            "apply_items",
            "provider_cache",
        ):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            count = (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] if exists else 0
            )
            counts[table] = int(count)
        if as_json:
            _emit_json("status", {"database_exists": True, "counts": counts})
        else:
            for table, count in counts.items():
                typer.echo(f"{table:20} {count}")
    finally:
        connection.close()


def _state_connection(config: Path | None) -> sqlite3.Connection:
    settings = load_config(config)
    if settings.database_path is None:
        raise typer.BadParameter("database path is required")
    from .db import migrate

    configure_logging(
        settings.log_level,
        _log_file(settings),
        secrets=provider_secrets(settings),
    )
    migrate(settings.database_path)
    return connect(settings.database_path)


def _plan_store(config: Path | None) -> tuple[sqlite3.Connection, PlanStore]:
    connection = _state_connection(config)
    return connection, PlanStore(connection)


def _apply_engine(config: Path | None) -> ApplyEngine:
    settings = load_config(config)
    configure_logging(
        settings.log_level,
        _log_file(settings),
        secrets=provider_secrets(settings),
    )
    return ApplyEngine(settings)


def _echo_apply_summary(summary: ApplySummary) -> None:
    typer.echo(f"Plan: {summary.plan_id}")
    typer.echo(f"Apply run: {summary.run_id}")
    typer.echo(f"Status: {summary.status.value}")
    for state, count in sorted(summary.counts.items()):
        typer.echo(f"{state:20} {count}")


def _echo_apply_preview(preview: ApplyPreview) -> None:
    typer.echo(f"Plan: {preview.plan_id}  Digest: {preview.digest[:12]}...")
    typer.echo(f"Items: {preview.item_count}  Metadata writes: {preview.metadata_write_count}")
    typer.echo(f"CBR -> CBZ: {preview.cbr_to_cbz_count}")
    typer.echo(f"Destination libraries: {', '.join(preview.destination_libraries)}")
    typer.echo(f"Estimated temporary space: {_human_bytes(preview.estimated_temporary_bytes)}")
    for policy, count in sorted(preview.lifecycle_counts.items()):
        warning = (
            " (incoming originals removed after verified commit)"
            if policy == "move_after_verify"
            else ""
        )
        typer.echo(f"{policy}: {count}{warning}")
    typer.echo(f"Blocking conflicts: {preview.conflict_count}")


@app.command("apply")
def apply_command(
    plan_id: int,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit versioned JSON.")] = False,
) -> None:
    """Execute an already approved immutable plan; no approval shortcut exists."""
    try:
        engine = _apply_engine(config)
        preview = engine.preview(plan_id)
        if not as_json:
            _echo_apply_preview(preview)
        summary = engine.apply(plan_id)
        if as_json:
            _emit_json("apply", {"preview": asdict(preview), "summary": asdict(summary)})
        else:
            _echo_apply_summary(summary)
    except (ApplyRefused, LockUnavailable) as exc:
        typer.echo(f"REFUSED: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command("recover")
def recover_command(
    plan_id: int,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit versioned JSON.")] = False,
) -> None:
    """Recover a prior apply run using only its immutable plan and durable journal."""
    try:
        summary = _apply_engine(config).recover(plan_id)
        if as_json:
            _emit_json("recover", {"summary": asdict(summary)})
        else:
            _echo_apply_summary(summary)
    except (ApplyRefused, LockUnavailable) as exc:
        typer.echo(f"RECOVERY REFUSED: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command("apply-status")
def apply_status_command(
    plan_id: int,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit versioned JSON.")] = False,
) -> None:
    """Show durable apply/recovery state without touching media or providers."""
    try:
        summary = _apply_engine(config).status(plan_id)
        inspections = _apply_engine(config).inspect_recovery(plan_id) if summary else ()
        if summary is None:
            connection, store = _plan_store(config)
            try:
                store.get(plan_id)
            finally:
                connection.close()
    except (ApplyRefused, KeyError, ValueError, RuntimeError) as exc:
        _expected_error(exc)
    if as_json:
        _emit_json(
            "apply-status",
            {
                "summary": asdict(summary) if summary else None,
                "friendly_counts": _friendly_counts(summary.counts) if summary else {},
                "items": [asdict(item) for item in inspections],
            },
        )
        return
    if summary is None:
        typer.echo(f"Plan {plan_id} has no apply run.")
    else:
        typer.echo(f"Plan: {summary.plan_id}\nApply run: {summary.run_id}\n")
        for label, count in _friendly_counts(summary.counts).items():
            typer.echo(f"{label:22} {count}")
        for item in inspections:
            staging_exists = bool(item.staging and Path(item.staging).exists())
            typer.echo(f"\n{item.item_id}: last durable state={item.state.value}")
            typer.echo(f"  source: {_evidence_label(item.source_exists, item.source_matches)}")
            typer.echo(f"  staging: {_evidence_label(staging_exists, item.staging_matches)}")
            typer.echo(
                "  destination: "
                f"{_evidence_label(item.destination_exists, item.destination_matches)}"
            )
            typer.echo(f"  proposed: {item.proposed_action}")
            typer.echo(
                "  intervention: "
                f"{'manual required' if item.manual_intervention else 'automatic proof available'}"
            )
            if item.detail:
                typer.echo(f"  detail: {item.detail}")


@app.command("rollback")
def rollback_command(
    plan_id: int,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Preview only provably reversible rollback actions; never execute them."""
    settings = load_config(config)
    if settings.database_path is None:
        raise typer.BadParameter("database path is required")
    for item in preview_rollback(settings.database_path, plan_id):
        marker = "REVERSIBLE" if item.reversible else "REFUSED"
        typer.echo(f"{marker:10} {item.item_id}: {item.action} - {item.explanation}")
    typer.echo("Preview only; no rollback filesystem action was executed.")


@plan_app.command("create")
def plan_create(
    root: Annotated[Path, typer.Argument(exists=True, file_okay=False, resolve_path=True)],
    config: Annotated[Path | None, typer.Option("--config")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Human-readable plan label.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit versioned JSON.")] = False,
) -> None:
    """Build a draft immutable plan from explicitly reviewed state under ROOT."""
    settings = load_config(config)
    connection = _state_connection(config)
    try:
        result = PlanBuilder(connection, settings).build(root, name=name)
        plan = PlanStore(connection).add(result.document)
        payload = {
            "plan_id": plan.id,
            "sha256": plan.sha256,
            "byte_length": plan.byte_length,
            "accepted_included": result.accepted_included,
            "work_only_included": result.work_only_included,
            "manual_included": result.manual_included,
            "unapproved_excluded": result.unapproved_excluded,
            "unresolved_blocked": result.unresolved_blocked,
            "skipped": result.skipped,
            "conflicts": result.conflicts,
            "exclusions": [asdict(item) for item in result.exclusions],
        }
        if as_json:
            _emit_json("plan-create", payload)
            return
        typer.echo(f"Created draft plan {plan.id} sha256={plan.sha256} bytes={plan.byte_length}")
        for key, value in payload.items():
            if key not in {"plan_id", "sha256", "byte_length", "exclusions"}:
                typer.echo(f"{key:24} {value}")
        for exclusion in result.exclusions:
            typer.echo(f"excluded {exclusion.category:16} {exclusion.path}")
            typer.echo(f"  {exclusion.explanation}")
        typer.echo("\nNext:")
        typer.echo(f"  kavita-ingest plan show {plan.id} --summary")
        typer.echo(f"  kavita-ingest plan approve {plan.id} --digest {plan.sha256}")
    except ValueError as exc:
        typer.echo(f"REFUSED: {exc}", err=True)
        typer.echo("No plan was created.", err=True)
        typer.echo(
            "Review and explicitly accept an identity, then run plan create again.", err=True
        )
        raise typer.Exit(2) from exc
    finally:
        connection.close()


@plan_app.command("show")
def plan_show(
    plan_id: int,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit versioned JSON.")] = False,
    summary_only: Annotated[
        bool, typer.Option("--summary", help="Show plan metadata without canonical item bytes.")
    ] = False,
) -> None:
    """Show authoritative plan bytes and approval state without network access."""
    connection, store = _plan_store(config)
    try:
        plan = store.get(plan_id)
        metadata = {
            "id": plan.id,
            "status": plan.status,
            "sha256": plan.sha256,
            "byte_length": plan.byte_length,
            "schema_version": plan.schema_version,
            "approved_at": plan.approved_at,
        }
        invalidated = connection.execute(
            "SELECT reason, invalidated_at FROM plan_invalidations WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        superseded = connection.execute(
            "SELECT new_plan_id, superseded_at FROM plan_supersessions WHERE old_plan_id=?",
            (plan_id,),
        ).fetchone()
        metadata["invalidation"] = dict(invalidated) if invalidated else None
        metadata["superseded_by"] = int(superseded[0]) if superseded else None
        metadata["superseded_at"] = str(superseded[1]) if superseded else None
        if as_json:
            payload: dict[str, object] = {"plan": metadata}
            if not summary_only:
                payload["document"] = json.loads(plan.canonical_json)
            _emit_json("plan-show", payload)
            return
        typer.echo(
            f"plan={plan.id} status={plan.status} sha256={plan.sha256} bytes={plan.byte_length}"
        )
        if invalidated:
            typer.echo(f"invalidated={invalidated['reason']} at {invalidated['invalidated_at']}")
        if superseded:
            typer.echo(
                f"superseded_by={superseded['new_plan_id']} at {superseded['superseded_at']}"
            )
        if not summary_only:
            typer.echo(plan.canonical_json.decode("utf-8"))
    except (KeyError, ValueError, RuntimeError) as exc:
        _expected_error(exc, hint="Use `kavita-ingest plan list` to see available plans.")
    finally:
        connection.close()


@plan_app.command("list")
def plan_list(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit versioned JSON.")] = False,
) -> None:
    """List immutable plans without requiring guessed identifiers."""
    connection, _ = _plan_store(config)
    try:
        rows = connection.execute(
            """
            SELECT p.id, p.status, p.created_at, p.approved_at, p.sha256,
                   count(i.item_id) AS item_count,
                   max(CASE WHEN x.plan_id IS NOT NULL THEN 1 ELSE 0 END) AS invalidated,
                   max(CASE WHEN s.old_plan_id IS NOT NULL THEN 1 ELSE 0 END) AS superseded
            FROM plans p
            LEFT JOIN plan_items_index i ON i.plan_id=p.id
            LEFT JOIN plan_invalidations x ON x.plan_id=p.id
            LEFT JOIN plan_supersessions s ON s.old_plan_id=p.id
            GROUP BY p.id ORDER BY p.id
            """
        ).fetchall()
        values = [dict(row) for row in rows]
        if as_json:
            _emit_json("plan-list", {"plans": values})
            return
        if not values:
            typer.echo("No plans exist.")
            return
        typer.echo("ID  Status    Created                    Approved  Flags         Items  Digest")
        for row in values:
            flags = ",".join(name for name in ("invalidated", "superseded") if row[name]) or "-"
            typer.echo(
                f"{row['id']:<3} {row['status']:<9} {str(row['created_at'])[:25]:<26} "
                f"{'yes' if row['approved_at'] else 'no':<9} {flags:<13} "
                f"{row['item_count']:<6} {str(row['sha256'])[:12]}"
            )
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
    except (KeyError, ValueError, RuntimeError) as exc:
        _expected_error(exc, hint="Use `kavita-ingest plan list` to see available plans.")
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
    except (KeyError, ValueError, RuntimeError) as exc:
        _expected_error(exc, hint="Use `kavita-ingest plan list` to see available plans.")
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
    except (OSError, ValueError, RuntimeError) as exc:
        _expected_error(exc)
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


def _emit_json(command: str, payload: dict[str, object]) -> None:
    typer.echo(
        json.dumps(
            {"output_version": OUTPUT_VERSION, "command": command, **payload},
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        )
    )


def _expected_error(exc: Exception, *, hint: str | None = None) -> None:
    typer.echo(f"REFUSED: {exc}.", err=True)
    if hint:
        typer.echo(hint, err=True)
    raise typer.Exit(2) from exc


def _candidate_detail(score: CandidateScore) -> str:
    candidate = score.candidate
    sequence = candidate.sequence
    issue = f" #{sequence.normalized}" if sequence else ""
    run = f"; run {candidate.run_start_year}" if candidate.run_start_year else ""
    date = f"; {candidate.publication_date}" if candidate.publication_date else ""
    return f"{candidate.series_title or candidate.title}{issue}{run}{date} ({score.score:.1f})"


def _log_file(settings: object) -> Path:
    database = getattr(settings, "database_path", None)
    return (
        Path(database).parent / "kavita-ingest.log"
        if database is not None
        else AppPaths.default().log_file
    )


def _friendly_counts(counts: dict[str, int]) -> dict[str, int]:
    return {
        "Complete": counts.get("complete", 0),
        "Pending": sum(
            counts.get(state, 0)
            for state in ("pending", "preflight_ok", "staging", "staged", "verified")
        ),
        "Committed": counts.get("committed", 0),
        "Cleanup pending": counts.get("cleanup_pending", 0) + counts.get("cleaned", 0),
        "Recovery required": counts.get("recovery_required", 0),
        "Failed/stale": counts.get("failed", 0) + counts.get("stale", 0),
    }


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def _evidence_label(exists: bool, matches: bool | None) -> str:
    if not exists:
        return "missing"
    if matches is True:
        return "present, hash matches durable evidence"
    if matches is False:
        return "present, HASH MISMATCH"
    return "present, no durable hash available"


def _workflow_menu() -> None:
    typer.echo("Kavita Ingest\n")
    typer.echo("[1] Scan incoming media")
    typer.echo("[2] Audit / match metadata")
    typer.echo("[3] Review identities")
    typer.echo("[4] Create / inspect ingestion plan")
    typer.echo("[5] Approve plan")
    typer.echo("[6] Apply approved plan")
    typer.echo("[7] Status / recovery")
    typer.echo("[8] Doctor")
    typer.echo("[Q] Quit")
    choice = typer.prompt("Select", default="Q").strip().casefold()
    if choice == "q":
        return
    if choice in {"1", "2", "3"}:
        root = Path(typer.prompt("Incoming directory")).expanduser().resolve()
        if choice == "1":
            scan(root, config=None, no_persist=False, as_json=False)
        elif choice == "2":
            audit_command(root, config=None, details=False, metrics=False, as_json=False)
        else:
            review_command(root, config=None)
        return
    if choice == "4":
        root = Path(typer.prompt("Reviewed incoming directory")).expanduser().resolve()
        plan_create(root, config=None, name=None, as_json=False)
        return
    if choice == "5":
        plan_id = typer.prompt("Plan ID", type=int)
        digest = typer.prompt("Exact displayed SHA-256 digest")
        plan_approve(plan_id, digest=digest, config=None)
        return
    if choice == "6":
        plan_id = typer.prompt("Approved plan ID", type=int)
        engine = _apply_engine(None)
        _echo_apply_preview(engine.preview(plan_id))
        if typer.confirm("Execute this approved immutable plan?", default=False):
            _echo_apply_summary(engine.apply(plan_id))
        return
    if choice == "7":
        plan_id = typer.prompt("Plan ID", type=int)
        apply_status_command(plan_id, config=None, as_json=False)
        if typer.confirm("Attempt safe recovery for this plan?", default=False):
            recover_command(plan_id, config=None, as_json=False)
        return
    if choice == "8":
        doctor(config=None, as_json=False)
        return
    typer.echo("Unknown selection.", err=True)
    raise typer.Exit(2)


if __name__ == "__main__":
    app()
