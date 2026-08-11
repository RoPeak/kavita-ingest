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
from rich.text import Text

from .apply_engine import ApplyEngine, ApplyRefused, ApplySummary
from .audit import AuditResult, run_audit
from .config import AppConfig
from .db import connect, migrate
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
            "SELECT plan_id, status FROM apply_runs WHERE status<>'complete' "
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
    return f"A previous plan became stale ({row['reason']}). A new plan is required."


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
            output.print("[R] Recover interrupted ingest   [D] Diagnostics   [Q] Quit")
            choice = _choice("R")
            if choice in {"", "R"}:
                return "resume"
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
    audit = run_audit(root, config, mode="wizard")
    _render_audit(audit, output)
    _stage(output, 2, "Discover", "complete")
    if not audit.items:
        output.print("No supported reading media was found. No changes made.")
        return None
    _stage(output, 3, "Review", "current")
    interactive_review(root, config, output, audit_result=audit, wizard_mode=True)
    _stage(output, 3, "Review", "saved")
    return _plan_reviewed(config, root, output)


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
            render_completed_apply(recovered, document, output)
            return _finish_menu(document, output)
        render_apply_summary(recovered, output)
        return None
    return _review_and_maybe_apply(config, _get_plan(config, state.plan_id), output)


def _review_and_maybe_apply(
    config: AppConfig, plan: StoredPlan, output: Console
) -> str | None:
    document = render_plan_summary(plan, output, technical_header=False)
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
    if len(config.incoming_roots) > 1:
        typer.echo("Configured incoming roots:")
        for index, root in enumerate(config.incoming_roots, 1):
            typer.echo(f"  [{index}] {root}")
        choice = int(typer.prompt("Choose root number, or 0 for another directory", type=int))
        if 1 <= choice <= len(config.incoming_roots):
            return config.incoming_roots[choice - 1].expanduser().resolve(strict=True)
    return Path(str(typer.prompt("Incoming directory"))).expanduser().resolve(strict=True)


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
        engine = ApplyEngine(config)
        preview = engine.preview(plan_id)
        output.print(f"Applying {preview.item_count} item{'s' if preview.item_count != 1 else ''}")
        summary = engine.apply(plan_id)
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
    render_completed_apply(summary, document, output)
    return summary


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
    count = summary["sources"]
    output.print(f"Found {count} supported file{'s' if count != 1 else ''}\n")
    output.print(f"Ready to confirm     {summary['eligible_high_confidence']}")
    output.print(f"Ambiguous            {summary['review_required']}")
    output.print(f"Unresolved           {summary['unresolved']}")
    output.print(f"Provider problems    {problems}")


def _render_build_result(result: PlanBuildResult, output: Console) -> None:
    output.print(
        f"Prepared {result.accepted_included} item{'s' if result.accepted_included != 1 else ''}; "
        f"left out {result.unapproved_excluded} unapproved, "
        f"{result.unresolved_blocked} blocked and {result.skipped} skipped."
    )
    for exclusion in result.exclusions:
        output.print(f"  {Path(exclusion.path).name}: {exclusion.explanation}")


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
    connection: sqlite3.Connection, config: AppConfig
) -> tuple[Path, int] | None:
    rows = connection.execute(
        "SELECT DISTINCT s.path FROM sources s JOIN decisions d ON d.source_fingerprint=s.sha256 "
        "WHERE d.id=(SELECT max(d2.id) FROM decisions d2 "
        "WHERE d2.source_fingerprint=d.source_fingerprint "
        "AND d2.media_signature=d.media_signature) "
        "AND d.decision_type IN ('accepted', 'work_accepted', 'manual_identity') "
        "AND NOT EXISTS (SELECT 1 FROM plan_items_index pi JOIN apply_runs a "
        "ON a.plan_id=pi.plan_id WHERE pi.source_fingerprint=s.sha256)"
    ).fetchall()
    paths = [Path(str(row["path"])).expanduser().resolve(strict=False) for row in rows]
    roots = [root.expanduser().resolve(strict=False) for root in config.incoming_roots]
    for root in roots:
        count = sum(path.is_relative_to(root) for path in paths)
        if count:
            return root, count
    if paths:
        return paths[0].parent, len(paths)
    return None


def _unresolved_review(
    connection: sqlite3.Connection, config: AppConfig
) -> tuple[Path, int] | None:
    rows = connection.execute(
        "SELECT DISTINCT s.path FROM sources s JOIN decisions d ON d.source_fingerprint=s.sha256 "
        "WHERE d.id=(SELECT max(d2.id) FROM decisions d2 "
        "WHERE d2.source_fingerprint=d.source_fingerprint "
        "AND d2.media_signature=d.media_signature) AND d.decision_type='unresolved'"
    ).fetchall()
    paths = [Path(str(row["path"])).expanduser().resolve(strict=False) for row in rows]
    for configured in config.incoming_roots:
        root = configured.expanduser().resolve(strict=False)
        count = sum(path.is_relative_to(root) for path in paths)
        if count:
            return root, count
    return (paths[0].parent, len(paths)) if paths else None


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
