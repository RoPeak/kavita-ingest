from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.text import Text

from .apply_engine import ApplyEngine, ApplyRefused, ApplySummary
from .audit import AuditResult, run_audit
from .completed_sources import CompletedSourceAssessment, assess_completed_sources
from .config import AppConfig
from .db import connect, migrate
from .decisions import DecisionRepository, decision_needs_review
from .doctor import Check, checks
from .paths import AppPaths
from .plan_store import PlanStore, StoredPlan
from .planning_service import NoActionableItems, PlanBuilder, PlanBuildResult
from .presentation import (
    plan_document,
    render_apply_summary,
    render_completed_apply,
    render_plan_details,
    render_plan_summary,
    render_technical_plan,
)
from .review import interactive_review
from .run_groups import run_group_key
from .scanner import ScanResult, scan


@dataclass(frozen=True, slots=True)
class ResumeState:
    kind: str
    plan_id: int | None
    detail: str
    root: Path | None = None
    item_count: int = 0

    @property
    def action_label(self) -> str:
        return {
            "review": "Resume review",
            "reviewed": "Resume and prepare plan",
            "draft": "Review draft plan",
            "approved": "Apply approved plan",
            "recovery": "Recover interrupted ingest",
        }.get(self.kind, "Resume previous work")


@dataclass(frozen=True, slots=True)
class DiscoverySelection:
    scans: tuple[ScanResult, ...]
    reprocess: bool = False


def run_wizard(
    config: AppConfig,
    *,
    config_path: Path | None = None,
    console: Console | None = None,
) -> None:
    output = console or Console()
    while True:
        resume = detect_resume_state(config)
        action = _home(config, resume, output, config_path)
        if action == "quit":
            output.print("No changes made.")
            return
        if action == "resume" and resume:
            result = _resume(config, resume, output)
        elif action == "abandon" and resume:
            _abandon_recovery(config, resume, output)
            continue
        else:
            root = _source_for_action(config, action)
            result = _run_new(config, root, config_path, output)
        if result != "new":
            return


def detect_resume_state(config: AppConfig) -> ResumeState | None:
    database = config.database_path
    if database is None or not database.exists():
        return None
    migrate(database)
    with connect(database) as connection:
        recovery = connection.execute(
            "SELECT plan_id, status FROM apply_runs "
            "WHERE status IN ('preflighting', 'running', 'recovery_required') "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if recovery:
            return ResumeState(
                "recovery",
                int(recovery["plan_id"]),
                "An interrupted ingest needs recovery before new work.",
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
            label = "A draft plan is ready to review." if status == "draft" else (
                "An approved plan is ready to apply."
            )
            return ResumeState(status, int(row["id"]), label)
        reviewed = _reviewed_without_plan(connection, config)
        if reviewed:
            root, count = reviewed
            noun = "item is" if count == 1 else "items are"
            return ResumeState(
                "reviewed",
                None,
                f"{count} reviewed {noun} ready to be planned.",
                root,
                count,
            )
        unresolved = _unresolved_review(connection, config)
        if unresolved:
            root, count = unresolved
            noun = "item needs" if count == 1 else "items need"
            return ResumeState(
                "review",
                None,
                f"{count} unresolved {noun} more review.",
                root,
                count,
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

        if _invalidation_replaced(connection, int(row["plan_id"])):
            return None

    reason = str(row["reason"])

    if reason.startswith("abandoned by user:"):
        detail = reason.removeprefix("abandoned by user:").strip()
        return (
            f"A previous ingest was abandoned ({detail}). "
            "A replacement plan is still required."
        )

    return (
        f"A previous plan became stale ({reason}). "
        "A replacement plan is still required."
    )


def _invalidation_replaced(
    connection: sqlite3.Connection,
    plan_id: int,
) -> bool:
    """Return whether all unfinished work has a later completed replacement."""
    planned = connection.execute(
        "SELECT item_id, source_fingerprint "
        "FROM plan_items_index WHERE plan_id=?",
        (plan_id,),
    ).fetchall()

    if not planned:
        return True

    latest_run = connection.execute(
        "SELECT id FROM apply_runs WHERE plan_id=? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (plan_id,),
    ).fetchone()

    if latest_run is None:
        required = {
            str(row["source_fingerprint"])
            for row in planned
        }
    else:
        states = {
            str(row["item_id"]): (
                str(row["state"])
                if row["state"] is not None
                else None
            )
            for row in connection.execute(
                "SELECT pi.item_id, ai.state "
                "FROM plan_items_index pi "
                "LEFT JOIN apply_items ai "
                "ON ai.run_id=? AND ai.item_id=pi.item_id "
                "WHERE pi.plan_id=?",
                (str(latest_run["id"]), plan_id),
            ).fetchall()
        }

        required = {
            str(row["source_fingerprint"])
            for row in planned
            if states.get(str(row["item_id"])) != "complete"
        }

    # A plan whose affected items all completed needs no replacement banner.
    if not required:
        return True

    placeholders = ",".join("?" for _ in required)

    rows = connection.execute(
        f"""
        SELECT DISTINCT pi.source_fingerprint
        FROM plan_items_index pi
        JOIN apply_runs ar ON ar.plan_id=pi.plan_id
        WHERE pi.plan_id>?
          AND ar.status='complete'
          AND pi.source_fingerprint IN ({placeholders})
        """,
        (plan_id, *sorted(required)),
    ).fetchall()

    replaced = {
        str(row["source_fingerprint"])
        for row in rows
    }

    return required.issubset(replaced)


def _home(
    config: AppConfig,
    resume: ResumeState | None,
    output: Console,
    config_path: Path | None,
) -> str:
    while True:
        output.print(Panel(Text(_configuration_summary(config)), title="Kavita Ingest"))
        notice = latest_invalidation_notice(config)
        if notice:
            output.print(notice)
        if resume and resume.kind == "recovery":
            output.print(f"\n{resume.detail}\n")
            output.print(
                "[R] Recover interrupted ingest   "
                "[A] Abandon this ingest   "
                "[D] Diagnostics   [Q] Quit"
            )
            choice = _choice("R")
            if choice in {"", "R"}:
                return "resume"
            if choice == "A":
                return "abandon"
        else:
            root = _single_valid_root(config)
            if root:
                output.print(f"\nStart ingest from:\n  {root}\n")
                options = ["[Enter] Start new ingest", "[C] Change source"]
            else:
                options = ["[Enter] Choose source and start"]
            if resume:
                output.print(f"{resume.detail}\n[R] {resume.action_label}")
            options.extend(["[D] Diagnostics", "[Q] Quit"])
            output.print("   ".join(options))
            choice = _choice("")
            if choice == "R" and resume:
                return "resume"
            if choice in {"", "S"}:
                return "start"
            if choice == "C":
                return "change"
        if choice == "D":
            _diagnostics(config, config_path, output)
            continue
        if choice == "Q":
            return "quit"
        output.print("Choose one of the displayed actions.")


def _run_new(
    config: AppConfig, root: Path, config_path: Path | None, output: Console
) -> str | None:
    _stage(output, 1, "Preflight", "current")
    _preflight(config, config_path, output)
    _stage(output, 1, "Preflight", "complete")
    _stage(output, 2, "Discover", "current")
    assessment = _completed_source_assessment(config, scan(root, config, persist=True))
    selection = _select_discovered_sources(assessment, output)
    if isinstance(selection, str):
        _stage(output, 2, "Discover", "complete")
        return "new"
    if selection is None:
        _stage(output, 2, "Discover", "complete")
        return None
    audit = run_audit(root, config, mode="wizard", scans_override=selection.scans)
    _render_audit(audit, output)
    _stage(output, 2, "Discover", "complete")
    if not audit.items:
        output.print("No supported reading media was found. No changes made.")
        return None
    _stage(output, 3, "Review", "current")
    decision_heads = _decision_heads(config, audit) if selection.reprocess else {}
    while True:
        incomplete = _incomplete_review_items(
            config, audit, required_newer_than=decision_heads
        )
        if not incomplete:
            break
        review_fingerprints = frozenset(item.source.sha256 for item in incomplete)
        run_group_heads = _run_group_heads(config, audit)
        interactive_review(
            root,
            config,
            output,
            audit_result=audit,
            wizard_mode=True,
            review_fingerprints=review_fingerprints,
        )
        if _run_group_heads(config, audit) != run_group_heads:
            output.print("\nRun choice changed; refreshing this review inside the selected run.\n")
            audit = run_audit(root, config, mode="wizard", scans_override=selection.scans)
            _render_audit(audit, output)
            continue
        incomplete = _incomplete_review_items(config, audit, required_newer_than=decision_heads)
        if not incomplete:
            break
        output.print("\nReview is incomplete.\n")
        count = len(incomplete)
        verb = "need" if count != 1 else "needs"
        output.print(f"{count} item{'s' if count != 1 else ''} still {verb} a decision.\n")
        output.print(
            "[R] Return to review   [P] Plan approved items only   [Q] Save and quit"
        )
        choice = _choice("Q")
        if choice == "P":
            if typer.confirm(
                f"Build a plan from explicitly approved items only and leave the other "
                f"{count} item{'s' if count != 1 else ''} untouched?",
                default=False,
            ):
                _stage(output, 3, "Review", "saved")
                return _plan_reviewed(config, root, output)
            continue
        if choice == "R":
            audit = run_audit(root, config, mode="wizard", scans_override=selection.scans)
            _render_audit(audit, output)
            continue
        output.print("Review decisions saved. Resume later to finish review.")
        return None
    _stage(output, 3, "Review", "saved")
    if len(audit.items) >= 10:
        _reader_pause(output, "Review complete. Press Enter to review the immutable plan")
    return _plan_reviewed(config, root, output)


def _run_group_heads(config: AppConfig, audit: AuditResult) -> dict[str, int]:
    if config.database_path is None:
        return {}
    keys = {
        run_group_key(item.local.series_title)
        for item in audit.items
        if item.local.kind.value == "comic" and item.local.series_title
    }
    if not keys:
        return {}
    connection = connect(config.database_path)
    try:
        output: dict[str, int] = {}
        for key in keys:
            row = connection.execute(
                "SELECT id FROM run_group_decisions "
                "WHERE group_key=? AND provider='comic_vine' ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
            if row is not None:
                output[key] = int(row["id"])
        return output
    finally:
        connection.close()


def _completed_source_assessment(
    config: AppConfig, scans: list[ScanResult]
) -> CompletedSourceAssessment:
    connection = _connection(config)
    try:
        return assess_completed_sources(connection, scans)
    finally:
        connection.close()


def _select_discovered_sources(
    assessment: CompletedSourceAssessment, output: Console
) -> DiscoverySelection | str | None:
    total = len(assessment.current) + len(assessment.completed)
    output.print(f"Found {total} supported file{'s' if total != 1 else ''}\n")
    output.print(f"New                 {len(assessment.current)}")
    output.print(f"Already ingested    {len(assessment.completed)}")
    for warning in assessment.warnings:
        label = (
            "destination is missing"
            if warning.condition == "destination_missing"
            else "destination no longer matches the verified output"
        )
        output.print(f"Warning: {warning.scan.source.path.name}: previously ingested, but {label}.")
    if not assessment.completed:
        return DiscoverySelection(assessment.current)
    if assessment.current:
        output.print(
            "\n[Enter] Continue with new/current work   "
            "[R] Reprocess completed   [Q] Quit"
        )
        choice = _choice("")
        if choice == "R":
            return DiscoverySelection(
                tuple(item.scan for item in assessment.completed), reprocess=True
            )
        if choice == "Q":
            return None
        return DiscoverySelection(assessment.current)
    output.print(
        "\nNothing new to ingest.\n\n"
        f"{len(assessment.completed)} unchanged source"
        f"{'s were' if len(assessment.completed) != 1 else ' was'} already successfully ingested."
    )
    while True:
        output.print(
            "[N] New/change source   [R] Reprocess completed item(s)   "
            "[D] Details   [Q] Quit"
        )
        choice = _choice("Q")
        if choice == "N":
            return "new"
        if choice == "R":
            return DiscoverySelection(
                tuple(item.scan for item in assessment.completed), reprocess=True
            )
        if choice == "D":
            for item in assessment.completed:
                output.print(f"{item.scan.source.path} -> {item.destination}")
            continue
        if choice in {"", "Q"}:
            return None
        output.print("Choose one of the displayed actions.")


def _decision_heads(config: AppConfig, audit: AuditResult) -> dict[str, int | None]:
    connection = _connection(config)
    try:
        decisions = DecisionRepository(connection)
        return {
            item.scan.source.sha256: decision.id if (decision := decisions.latest(
                item.scan.source
            )) else None
            for item in audit.items
        }
    finally:
        connection.close()


def _incomplete_review_items(
    config: AppConfig,
    audit: AuditResult,
    *,
    required_newer_than: dict[str, int | None] | None = None,
) -> tuple[ScanResult, ...]:
    connection = _connection(config)
    try:
        decisions = DecisionRepository(connection)
        incomplete = []
        baseline = required_newer_than or {}
        for item in audit.items:
            source = item.scan.source
            decision = decisions.latest(source)
            if decision_needs_review(
                decision,
                item.local.evidence_hash(),
                required_newer_than=baseline.get(source.sha256),
                require_newer=source.sha256 in baseline,
            ):
                incomplete.append(item.scan)
        return tuple(incomplete)
    finally:
        connection.close()


def _plan_reviewed(config: AppConfig, root: Path, output: Console) -> str | None:
    _stage(output, 4, "Plan", "current")
    try:
        plan, result = _create_plan(config, root)
    except NoActionableItems as exc:
        output.print(f"No plan was created: {exc}")
        output.print("Review remains saved; resolve or explicitly skip outstanding items.")
        return None
    _render_build_result(result, output)
    _stage(output, 4, "Plan", "complete")
    return _review_and_maybe_apply(config, plan, output)


def _abandon_recovery(
    config: AppConfig,
    state: ResumeState,
    output: Console,
) -> None:
    if state.plan_id is None:
        output.print("No apply run is available to abandon.")
        return

    output.print(
        "\nAbandoning closes this apply run and invalidates its immutable plan."
    )
    output.print(
        "No source, staging, or destination files will be moved, deleted, or restored."
    )
    output.print(
        "Runs with uncertain commit/cleanup state cannot be abandoned this way.\n"
    )

    if not typer.confirm(
        f"Abandon Plan {state.plan_id} and start over?",
        default=False,
    ):
        output.print("Abandonment cancelled.")
        return

    reason = str(
        typer.prompt(
            "Reason",
            default="user chose to start over",
        )
    )

    try:
        summary = ApplyEngine(config).abandon(
            state.plan_id,
            reason=reason,
        )
    except ApplyRefused as exc:
        output.print(f"Abandon refused: {exc}")
        return

    output.print(
        f"Plan {summary.plan_id} closed as abandoned. "
        "Its history has been preserved."
    )
    output.print("No media files were modified.")


def _resume(config: AppConfig, state: ResumeState, output: Console) -> str | None:
    if state.kind == "review":
        if state.root is None:
            return None
        return _run_new(config, state.root, None, output)
    if state.kind == "reviewed":
        if state.root is None:
            output.print("Reviewed work has no usable source root; start a new scan.")
            return None
        output.print("Using saved review decisions; providers will not be queried again.")
        return _plan_reviewed(config, state.root, output)
    if state.plan_id is None:
        return None
    if state.kind == "recovery":
        engine = ApplyEngine(config)
        summary = engine.status(state.plan_id)
        if summary:
            render_apply_summary(summary, output)
        if not typer.confirm("Recover this interrupted ingest now?", default=False):
            return None
        recovered = engine.recover(state.plan_id)
        plan = _get_plan(config, state.plan_id)
        document = plan_document(plan)
        if recovered.status.value == "complete":
            render_completed_apply(recovered, document, output, compact=True)
            return _finish_menu(document, output)
        render_apply_summary(recovered, output)
        return None
    return _review_and_maybe_apply(config, _get_plan(config, state.plan_id), output)


def _review_and_maybe_apply(
    config: AppConfig, plan: StoredPlan, output: Console
) -> str | None:
    document = render_plan_summary(
        plan,
        output,
        technical_header=False,
        pause_every=8 if output.is_terminal else None,
    )
    if plan.status == "draft":
        outcome = _plan_approval_menu(config, plan, output)
        if outcome != "approved":
            return "new" if outcome == "back" else None
        plan = _get_plan(config, plan.id)
        document = plan_document(plan)
        _stage(output, 5, "Approve", "complete")
        output.print("Plan approved. The exact displayed plan digest is now locked.")
    if not typer.confirm("Apply it now?", default=False):
        output.print("Approved plan saved. Resume later when you are ready to apply it.")
        return None
    _confirm_lifecycle(document)
    summary = _apply(config, plan.id, document, output)
    if summary is None or summary.status.value != "complete":
        return None
    return _finish_menu(document, output)


def _plan_approval_menu(
    config: AppConfig, displayed: StoredPlan, output: Console
) -> str:
    while True:
        _stage(output, 5, "Approve", "current")
        output.print(
            "[A] Approve this exact plan   [V] View metadata/details\n"
            "[T] View technical immutable plan   [B] Back   [Q] Save and quit"
        )
        choice = _choice("Q")
        if choice == "V":
            render_plan_details(displayed, output)
            continue
        if choice == "T":
            render_technical_plan(displayed, output)
            continue
        if choice == "B":
            return "back"
        if choice == "Q":
            output.print("Draft plan saved for later review.")
            return "quit"
        if choice == "A":
            current = _get_plan(config, displayed.id)
            if (
                current.sha256 != displayed.sha256
                or current.canonical_json != displayed.canonical_json
            ):
                output.print("The persisted plan changed and must be displayed again.")
                return "quit"
            try:
                _approve(config, current)
            except ValueError as exc:
                output.print(f"Plan cannot be approved: {exc}")
                return "quit"
            return "approved"
        output.print("Choose one of the displayed actions.")


def _source_for_action(config: AppConfig, action: str) -> Path:
    if action == "start":
        root = _single_valid_root(config)
        if root:
            return root
    return _select_root(config)


def _select_root(config: AppConfig) -> Path:
    while True:
        selected: Path | None = None
        if len(config.incoming_roots) > 1:
            typer.echo("Configured incoming roots:")
            for index, root in enumerate(config.incoming_roots, 1):
                typer.echo(f"  [{index}] {root}")
            choice = int(
                typer.prompt("Choose root number, or 0 for another directory", type=int)
            )
            if 1 <= choice <= len(config.incoming_roots):
                selected = config.incoming_roots[choice - 1]
        if selected is None:
            selected = Path(str(typer.prompt("Incoming directory")))

        candidate = selected.expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            typer.echo(f"Directory does not exist or cannot be resolved: {candidate}")
            continue
        if not resolved.is_dir():
            typer.echo(f"Path is not a directory: {resolved}")
            continue
        return resolved


def _single_valid_root(config: AppConfig) -> Path | None:
    if len(config.incoming_roots) != 1:
        return None
    root = config.incoming_roots[0].expanduser().resolve(strict=False)
    return root if root.is_dir() else None


def _preflight(config: AppConfig, config_path: Path | None, output: Console) -> None:
    results = checks(config, AppPaths.default(), config_path)
    blockers = [check for check in results if check.status == "BLOCKED"]
    warnings = [check for check in results if check.status == "WARN"]
    output.print(
        f"{len(results) - len(blockers) - len(warnings)} checks OK; "
        f"{len(warnings)} warnings; {len(blockers)} blockers"
    )
    for check in (*warnings, *blockers):
        output.print(f"{check.status}: {check.name}: {check.detail}")
    if blockers:
        if typer.confirm("Show all diagnostic details?", default=False):
            _render_checks(results, output)
        raise typer.BadParameter("doctor preflight has blocking checks")


def _diagnostics(config: AppConfig, config_path: Path | None, output: Console) -> None:
    output.rule("Diagnostics")
    _render_checks(checks(config, AppPaths.default(), config_path), output)


def _create_plan(config: AppConfig, root: Path) -> tuple[StoredPlan, PlanBuildResult]:
    connection = _connection(config)
    try:
        result = PlanBuilder(connection, config).build(root)
        return PlanStore(connection).add(result.document), result
    finally:
        connection.close()


def _approve(config: AppConfig, plan: StoredPlan) -> StoredPlan:
    connection = _connection(config)
    try:
        current = PlanStore(connection).get(plan.id)
        if current.sha256 != plan.sha256 or current.canonical_json != plan.canonical_json:
            raise ValueError("persisted plan changed; display it again before approval")
        return PlanStore(connection).approve(current.id, current.sha256)
    finally:
        connection.close()


def _get_plan(config: AppConfig, plan_id: int) -> StoredPlan:
    connection = _connection(config)
    try:
        return PlanStore(connection).get(plan_id)
    finally:
        connection.close()


def _apply(
    config: AppConfig,
    plan_id: int,
    document: dict[str, Any],
    output: Console,
) -> ApplySummary | None:
    _stage(output, 6, "Apply", "current")
    try:
        preview = ApplyEngine(config).preview(plan_id)
        output.print(f"Applying {preview.item_count} item{'s' if preview.item_count != 1 else ''}")
        if output.is_terminal and preview.item_count:
            with Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                console=output,
                transient=False,
            ) as progress:
                task = progress.add_task("Starting Apply", total=preview.item_count)

                def update(completed: int, total: int, source_name: str) -> None:
                    label = _short_progress_name(source_name)
                    progress.update(
                        task,
                        completed=completed,
                        total=total,
                        description=f"Applying {label}" if label else "Applying",
                    )

                summary = ApplyEngine(config, progress=update).apply(plan_id)
        else:
            summary = ApplyEngine(config).apply(plan_id)
    except ApplyRefused as exc:
        output.print(f"Apply refused: {exc}")
        return None
    if summary.status.value != "complete":
        render_apply_summary(summary, output)
        return summary
    _stage(output, 6, "Apply", "complete")
    _stage(output, 7, "Finish", "complete")
    output.print("✓ Source verified\n✓ Metadata written\n✓ Staged output verified")
    output.print("✓ Published\n✓ Destination verified\n✓ Source lifecycle completed")
    render_completed_apply(summary, document, output, compact=True)
    return summary


def _short_progress_name(value: str, *, limit: int = 54) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _reader_pause(output: Console, message: str) -> None:
    if not output.is_terminal:
        return
    typer.prompt(message, default="", show_default=False)


def _finish_menu(document: dict[str, Any], output: Console) -> str | None:
    options = ["[N] New ingest", "[D] Details"]
    open_root = _openable_root(document)
    if open_root:
        options.append("[O] Open destination folder")
    options.append("[Q] Quit")
    output.print("   ".join(options))
    while True:
        choice = _choice("Q")
        if choice == "N":
            return "new"
        if choice == "D":
            for item in _document_destinations(document):
                output.print(item)
            return None
        if choice == "O" and open_root:
            subprocess.run(["xdg-open", str(open_root)], check=False)
            return None
        if choice in {"", "Q"}:
            return None
        output.print("Choose one of the displayed actions.")


def _confirm_lifecycle(document: dict[str, Any]) -> None:
    policies = {
        str(item.get("lifecycle_actions", [{}])[-1].get("action"))
        for item in document.get("items", [])
        if isinstance(item, dict)
    }
    if (
        "move_after_verify" in policies or "remove_source_after_verified_commit" in policies
    ) and not typer.confirm(
        "Incoming sources will be removed only after verified publication. Continue?",
        default=False,
    ):
        raise typer.Abort()
    if "archive_after_verify" in policies and not typer.confirm(
        "Incoming sources will be archived only after verified publication. Continue?",
        default=False,
    ):
        raise typer.Abort()


def _render_audit(audit: AuditResult, output: Console) -> None:
    summary = audit.summary
    problems = summary["provider_unavailable"] + summary["partial_provider_unavailable"]
    output.print(f"Ready to confirm     {summary['eligible_high_confidence']}")
    output.print(f"Ambiguous            {summary['review_required']}")
    output.print(f"Unresolved           {summary['unresolved']}")
    output.print(f"Provider problems    {problems}")


def _render_build_result(result: PlanBuildResult, output: Console) -> None:
    unresolved = sum(
        1
        for item in result.exclusions
        if item.category == "unresolved"
    )
    skipped = sum(
        1
        for item in result.exclusions
        if item.category == "skipped"
    )
    historical = tuple(
        item
        for item in result.exclusions
        if item.category == "historical_missing"
    )
    actionable = tuple(
        item
        for item in result.exclusions
        if item.category != "historical_missing"
    )

    output.print(
        f"Prepared {result.accepted_included} "
        f"item{'s' if result.accepted_included != 1 else ''}; "
        f"left out {result.unapproved_excluded} unapproved, "
        f"{unresolved} unresolved, "
        f"{result.unresolved_blocked} blocked and "
        f"{skipped} skipped."
    )

    if historical:
        noun = "record" if len(historical) == 1 else "records"
        output.print(
            f"Ignored {len(historical)} historical source {noun} "
            "no longer present."
        )

    for exclusion in actionable:
        output.print(
            f"  {Path(exclusion.path).name}: "
            f"{exclusion.explanation}"
        )


def _configuration_summary(config: AppConfig) -> str:
    incoming = ", ".join(str(path) for path in config.incoming_roots) or "choose at runtime"
    lifecycle = {
        "preserve": "Preserve",
        "move_after_verify": "Remove after verify",
        "archive_after_verify": "Archive after verify",
    }.get(config.source_lifecycle, config.source_lifecycle)
    return (
        f"Incoming   {incoming}\nComics     {config.comics_root or 'not configured'}\n"
        f"Books      {config.books_root or 'not configured'}\nSource     {lifecycle}"
    )


def _reviewed_without_plan(
    connection: sqlite3.Connection,
    config: AppConfig,
) -> tuple[Path, int] | None:
    rows = connection.execute(
        """
        SELECT DISTINCT s.path
        FROM sources s
        JOIN decisions d
          ON d.source_fingerprint=s.sha256
         AND d.media_signature=(s.format || ':' || s.size)
        WHERE d.id=(
            SELECT max(d2.id)
            FROM decisions d2
            WHERE d2.source_fingerprint=d.source_fingerprint
              AND d2.media_signature=d.media_signature
        )
          AND (
              d.decision_type IN (
                  'accepted',
                  'work_accepted',
                  'manual_identity'
              )
              OR (
                  d.decision_type='manual_override'
                  AND EXISTS (
                      SELECT 1
                      FROM decisions auth
                      WHERE auth.source_fingerprint=d.source_fingerprint
                        AND auth.media_signature=d.media_signature
                        AND auth.id<=d.id
                        AND auth.decision_type IN (
                            'accepted',
                            'work_accepted',
                            'manual_identity'
                        )
                  )
              )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM plan_preconditions pp
              JOIN apply_runs a ON a.plan_id=pp.plan_id
              WHERE pp.decision_head_id=d.id
          )
        """
    ).fetchall()
    paths = [
        Path(str(row["path"])).expanduser().resolve(strict=False)
        for row in rows
    ]
    return _narrow_resume_scope(paths, config)


def _unresolved_review(
    connection: sqlite3.Connection,
    config: AppConfig,
) -> tuple[Path, int] | None:
    rows = connection.execute(
        """
        SELECT DISTINCT s.path
        FROM sources s
        JOIN decisions d
          ON d.source_fingerprint=s.sha256
         AND d.media_signature=(s.format || ':' || s.size)
        WHERE d.id=(
            SELECT max(d2.id)
            FROM decisions d2
            WHERE d2.source_fingerprint=d.source_fingerprint
              AND d2.media_signature=d.media_signature
        )
          AND d.decision_type='unresolved'
        """
    ).fetchall()
    paths = [
        Path(str(row["path"])).expanduser().resolve(strict=False)
        for row in rows
    ]
    return _narrow_resume_scope(paths, config)


def _narrow_resume_scope(
    paths: list[Path],
    config: AppConfig,
) -> tuple[Path, int] | None:
    if not paths:
        return None

    existing = [path for path in paths if path.exists()]
    candidates = existing or paths

    for configured in config.incoming_roots:
        root = configured.expanduser().resolve(strict=False)
        subset = [
            path
            for path in candidates
            if path.is_relative_to(root)
        ]
        if not subset:
            continue

        common = Path(
            os.path.commonpath(
                [str(path.parent) for path in subset]
            )
        )
        return common, len(subset)

    common = Path(
        os.path.commonpath(
            [str(path.parent) for path in candidates]
        )
    )
    return common, len(candidates)



def _connection(config: AppConfig) -> sqlite3.Connection:
    if config.database_path is None:
        raise ValueError("wizard requires a state database")
    migrate(config.database_path)
    return connect(config.database_path)


def _render_checks(results: tuple[Check, ...], output: Console) -> None:
    for check in results:
        output.print(f"{check.status:7} {check.category}/{check.name}: {check.detail}")


def _stage(output: Console, number: int, name: str, state: str) -> None:
    marker = {"current": "current", "complete": "✓", "saved": "saved"}[state]
    output.print(f"[{number}/7] {name:<12} {marker}", markup=False)


def _choice(default: str) -> str:
    return str(typer.prompt("Action", default=default, show_default=False)).strip().upper()


def _document_destinations(document: dict[str, Any]) -> list[str]:
    output = []
    for item in document.get("items", []):
        if isinstance(item, dict) and isinstance(item.get("kavita_projection"), dict):
            output.append(str(item["kavita_projection"].get("absolute_destination", "")))
    return [value for value in output if value]


def _openable_root(document: dict[str, Any]) -> Path | None:
    if not os.environ.get("DISPLAY") or shutil.which("xdg-open") is None:
        return None
    roots = {
        Path(str(item["kavita_projection"]["library_root"]))
        for item in document.get("items", [])
        if isinstance(item, dict)
        and isinstance(item.get("kavita_projection"), dict)
        and item["kavita_projection"].get("library_root")
    }
    return next(iter(roots)) if len(roots) == 1 else None
