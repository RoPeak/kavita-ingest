from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console

from .apply_engine import ApplyEngine, ApplyRefused
from .audit import AuditResult, run_audit
from .config import AppConfig
from .db import connect, migrate
from .doctor import Check, checks
from .paths import AppPaths
from .plan_store import PlanStore, StoredPlan
from .planning_service import NoActionableItems, PlanBuilder, PlanBuildResult
from .presentation import render_apply_summary, render_plan_summary
from .review import interactive_review


@dataclass(frozen=True, slots=True)
class ResumeState:
    kind: str
    plan_id: int
    detail: str


def run_wizard(
    config: AppConfig,
    *,
    config_path: Path | None = None,
    console: Console | None = None,
) -> None:
    output = console or Console()
    output.rule("Kavita Ingest")
    output.print(_configuration_summary(config))
    notice = latest_invalidation_notice(config)
    if notice:
        output.print(f"\nPrevious plan note: {notice}")
    resume = detect_resume_state(config)
    if resume:
        output.print(f"\nResume available: {resume.detail}")
        if typer.confirm("Resume this workflow?", default=True):
            _resume(config, resume, output)
            return
    if not typer.confirm("Start a new guided ingest?", default=True):
        output.print("No changes made.")
        return
    root = _select_root(config)
    _preflight(config, config_path, output)
    output.rule("Discover And Audit")
    audit = run_audit(root, config, mode="wizard")
    _render_audit(audit, output)
    if not audit.items:
        output.print("No supported reading media was found. No changes made.")
        return
    output.rule("Review")
    interactive_review(root, config, output, audit_result=audit)
    try:
        plan, result = _create_plan(config, root)
    except NoActionableItems as exc:
        output.print(f"No plan was created: {exc}")
        output.print("Review remains saved; resolve or explicitly skip outstanding items.")
        return
    _render_build_result(result, output)
    output.rule("Immutable Plan")
    document = render_plan_summary(plan, output)
    if not typer.confirm(
        f"Approve this exact immutable plan ({plan.sha256[:12]}...)?", default=False
    ):
        output.print(f"Draft plan {plan.id} saved. Resume later to approve or inspect it.")
        return
    plan = _approve(config, plan)
    output.print(f"Approved plan {plan.id}; full digest was bound internally.")
    if not typer.confirm("Apply the approved plan now?", default=False):
        output.print(f"Approved plan {plan.id} is ready to resume later.")
        return
    _confirm_lifecycle(document)
    _apply(config, plan.id, output)


def detect_resume_state(config: AppConfig) -> ResumeState | None:
    database = config.database_path
    if database is None or not database.exists():
        return None
    migrate(database)
    with connect(database) as connection:
        recovery = connection.execute(
            "SELECT plan_id, status FROM apply_runs WHERE status<>'complete' "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if recovery:
            return ResumeState(
                "recovery", int(recovery["plan_id"]),
                f"plan {recovery['plan_id']} has an incomplete apply ({recovery['status']})",
            )
        row = connection.execute(
            "SELECT p.id, p.status FROM plans p "
            "WHERE NOT EXISTS (SELECT 1 FROM apply_runs a WHERE a.plan_id=p.id) "
            "AND NOT EXISTS (SELECT 1 FROM plan_invalidations x WHERE x.plan_id=p.id) "
            "AND NOT EXISTS (SELECT 1 FROM plan_supersessions s WHERE s.old_plan_id=p.id) "
            "ORDER BY p.id DESC LIMIT 1"
        ).fetchone()
        if row:
            status = str(row["status"])
            return ResumeState(
                status, int(row["id"]),
                f"plan {row['id']} is {status} and has not been applied",
            )
    return None


def latest_invalidation_notice(config: AppConfig) -> str | None:
    database = config.database_path
    if database is None or not database.exists():
        return None
    migrate(database)
    with connect(database) as connection:
        row = connection.execute(
            "SELECT x.plan_id, x.reason FROM plan_invalidations x "
            "ORDER BY x.invalidated_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return f"plan {row['plan_id']} was invalidated ({row['reason']}); create a new plan"


def _resume(config: AppConfig, state: ResumeState, output: Console) -> None:
    if state.kind == "recovery":
        engine = ApplyEngine(config)
        summary = engine.status(state.plan_id)
        if summary:
            render_apply_summary(summary, output)
        if typer.confirm("Attempt evidence-based recovery now?", default=False):
            render_apply_summary(engine.recover(state.plan_id), output)
        return
    connection = _connection(config)
    try:
        store = PlanStore(connection)
        plan = store.get(state.plan_id)
        document = render_plan_summary(plan, output)
        if plan.status == "draft":
            invalidation = connection.execute(
                "SELECT reason FROM plan_invalidations WHERE plan_id=?", (plan.id,)
            ).fetchone()
            if invalidation:
                output.print(f"This plan is stale: {invalidation['reason']}. Create a new plan.")
                return
            if not typer.confirm(
                f"Approve this exact immutable plan ({plan.sha256[:12]}...)?", default=False
            ):
                return
            plan = store.approve(plan.id, plan.sha256)
            output.print(f"Approved plan {plan.id}; full digest was bound internally.")
    finally:
        connection.close()
    if typer.confirm("Apply the approved plan now?", default=False):
        _confirm_lifecycle(document)
        _apply(config, state.plan_id, output)


def _select_root(config: AppConfig) -> Path:
    if not config.incoming_roots:
        return Path(str(typer.prompt("Incoming directory"))).expanduser().resolve(strict=True)
    if len(config.incoming_roots) == 1:
        configured = config.incoming_roots[0].expanduser().resolve(strict=True)
        if typer.confirm(f"Use configured incoming root {configured}?", default=True):
            return configured
        return Path(str(typer.prompt("Temporary incoming directory"))).expanduser().resolve(
            strict=True
        )
    typer.echo("Configured incoming roots:")
    for index, root in enumerate(config.incoming_roots, 1):
        typer.echo(f"  [{index}] {root}")
    choice = int(typer.prompt("Choose root number, or 0 for a temporary root", type=int))
    if 1 <= choice <= len(config.incoming_roots):
        return config.incoming_roots[choice - 1].expanduser().resolve(strict=True)
    return Path(str(typer.prompt("Temporary incoming directory"))).expanduser().resolve(
        strict=True
    )


def _preflight(config: AppConfig, config_path: Path | None, output: Console) -> None:
    results = checks(config, AppPaths.default(), config_path)
    blockers = [check for check in results if check.status == "BLOCKED"]
    warnings = [check for check in results if check.status == "WARN"]
    output.rule("Preflight")
    output.print(
        f"{len(results) - len(blockers) - len(warnings)} checks OK; "
        f"{len(warnings)} warnings; {len(blockers)} blockers"
    )
    for check in (*warnings, *blockers):
        output.print(f"{check.status}: {check.name}: {check.detail}")
    if blockers:
        if typer.confirm("Show all doctor details?", default=False):
            _render_checks(results, output)
        raise typer.BadParameter("doctor preflight has blocking checks")


def _create_plan(config: AppConfig, root: Path) -> tuple[StoredPlan, PlanBuildResult]:
    connection = _connection(config)
    try:
        result = PlanBuilder(connection, config).build(root)
        return PlanStore(connection).add(result.document), result
    except NoActionableItems:
        raise
    finally:
        connection.close()


def _approve(config: AppConfig, plan: StoredPlan) -> StoredPlan:
    connection = _connection(config)
    try:
        return PlanStore(connection).approve(plan.id, plan.sha256)
    finally:
        connection.close()


def _apply(config: AppConfig, plan_id: int, output: Console) -> None:
    output.rule("Apply")
    output.print("Preflighting the complete immutable plan...")
    try:
        engine = ApplyEngine(config)
        preview = engine.preview(plan_id)
        output.print(
            f"Staging and verifying {preview.item_count} item(s); "
            "publication is atomic and no-clobber."
        )
        summary = engine.apply(plan_id)
    except ApplyRefused as exc:
        output.print(f"Apply refused: {exc}")
        return
    render_apply_summary(summary, output)
    output.print("Finished from durable journal state. Kavita can now scan the destinations.")


def _confirm_lifecycle(document: dict[str, object]) -> None:
    raw_items = document.get("items", [])
    items = raw_items if isinstance(raw_items, list) else []
    policies = {
        str(item.get("lifecycle_actions", [{}])[-1].get("action"))
        for item in items
        if isinstance(item, dict)
    }
    if (
        "move_after_verify" in policies or "remove_source_after_verified_commit" in policies
    ) and not typer.confirm(
        "Sources will be removed only after verified destination commit. Continue?",
        default=False,
    ):
        raise typer.Abort()
    if "archive_after_verify" in policies and not typer.confirm(
        "Sources will be archived only after verified destination commit. Continue?",
        default=False,
    ):
        raise typer.Abort()


def _render_audit(audit: AuditResult, output: Console) -> None:
    summary = audit.summary
    problems = summary["provider_unavailable"] + summary["partial_provider_unavailable"]
    output.print(
        f"Found {summary['sources']} source(s): "
        f"{summary['eligible_high_confidence']} strong match(es), "
        f"{summary['review_required']} need review, {summary['unresolved']} unresolved."
    )
    if problems:
        output.print(f"Provider availability affected {problems} source(s).")


def _render_build_result(result: PlanBuildResult, output: Console) -> None:
    output.print(
        f"Planned {result.accepted_included} item(s); excluded "
        f"{result.unapproved_excluded} unapproved, {result.unresolved_blocked} blocked, "
        f"{result.skipped} skipped."
    )
    for exclusion in result.exclusions:
        output.print(f"  Excluded {Path(exclusion.path).name}: {exclusion.explanation}")


def _configuration_summary(config: AppConfig) -> str:
    incoming = ", ".join(str(path) for path in config.incoming_roots) or "choose at runtime"
    return (
        f"Incoming: {incoming}\nBooks: {config.books_root or 'not configured'}\n"
        f"Comics: {config.comics_root or 'not configured'}\n"
        f"Source lifecycle: {config.source_lifecycle}"
    )


def _connection(config: AppConfig) -> sqlite3.Connection:
    if config.database_path is None:
        raise ValueError("wizard requires a state database")
    migrate(config.database_path)
    return connect(config.database_path)


def _render_checks(results: tuple[Check, ...], output: Console) -> None:
    for check in results:
        output.print(f"{check.status:7} {check.category}/{check.name}: {check.detail}")
