from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import subprocess
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

import pikepdf
import rarfile

from .apply_journal import (
    ApplyItem,
    ApplyRun,
    ItemState,
    JournalRepository,
    RunState,
)
from .archive_safety import ArchiveLimits, validate_inventory
from .calibre import require_safe_calibre_executable
from .config import AppConfig
from .db import connect, migrate
from .discovery import detect_signature
from .filesystem import DestinationExists, LinuxFilesystem, NoClobberFilesystem, sha256_file
from .locking import ProcessLock, lock_path
from .plan_store import PlanStore, StoredPlan
from .planning import (
    LEGACY_POLICY_MESSAGE,
    PLAN_SCHEMA_VERSION,
    SUPPORTED_PLAN_SCHEMA_VERSIONS,
    require_current_planning_policy,
    validate_plan_payload,
)
from .writers.comic import verify_cbz, write_cbz_metadata
from .writers.common import VerificationResult
from .writers.epub import CALIBRE_FIELDS, verify_epub, write_epub
from .writers.pdf import require_pdf_write_eligible, verify_pdf, write_pdf_metadata
from .writers.repack import repack_cbr_to_cbz


class ApplyRefused(RuntimeError):
    pass


class RecoveryRequired(ApplyRefused):
    pass


class PlanAlreadyComplete(ApplyRefused):
    pass


class StalePlan(ApplyRefused):
    pass


class InjectedCrash(BaseException):
    """Fault-injection crash that deliberately bypasses ordinary error handling."""


class FaultHook(Protocol):
    def __call__(self, checkpoint: str, item_id: str) -> None: ...


class DiskSpace(Protocol):
    free: int


def _no_fault(checkpoint: str, item_id: str) -> None:
    del checkpoint, item_id


def _disk_usage(path: Path) -> DiskSpace:
    return cast(DiskSpace, shutil.disk_usage(path))


@dataclass(frozen=True, slots=True)
class PreparedItem:
    item_id: str
    document: dict[str, Any]
    source: Path
    source_hash: str
    source_size: int
    source_format: str
    source_signature: str
    destination: Path
    lifecycle_policy: str
    archive_path: Path | None
    staging_directory: Path
    published_file_mode: int
    created_directory_mode: int


@dataclass(frozen=True, slots=True)
class ApplySummary:
    run_id: str
    plan_id: int
    status: RunState
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class ApplyPreview:
    plan_id: int
    digest: str
    item_count: int
    destination_libraries: tuple[str, ...]
    metadata_write_count: int
    cbr_to_cbz_count: int
    lifecycle_counts: dict[str, int]
    conflict_count: int
    estimated_temporary_bytes: int


@dataclass(frozen=True, slots=True)
class RecoveryInspection:
    item_id: str
    state: ItemState
    source: str
    source_exists: bool
    source_matches: bool | None
    staging: str | None
    staging_matches: bool | None
    destination: str
    destination_exists: bool
    destination_matches: bool | None
    proposed_action: str
    manual_intervention: bool
    detail: str | None


class WriterDispatcher:
    def stage(self, item: PreparedItem, destination: Path) -> VerificationResult:
        owned = item.document["ownership_manifest"]
        set_fields = owned.get("set", {})
        clear_fields = tuple(str(value) for value in owned.get("clear", []))
        if item.source_format == "epub":
            work_only = item.document.get("partial_resolution", {}).get("level") == "work_only"
            native = (
                {key: value for key, value in set_fields.items() if key in {"title", "authors"}}
                if work_only
                else {}
            )
            calibre = {
                key: value
                for key, value in set_fields.items()
                if key in CALIBRE_FIELDS and key not in native
            }
            exact_date = str(set_fields["date"]) if set_fields.get("date") else None
            contributors = _epub_roles(item.document)
            return write_epub(
                item.source,
                destination,
                calibre_fields=calibre,
                exact_date=exact_date,
                contributor_roles=contributors,
                native_fields=native,
            )
        if item.source_format == "cbz":
            return write_cbz_metadata(
                item.source, destination, set_fields=set_fields, clear_fields=clear_fields
            )
        if item.source_format == "cbr":
            return repack_cbr_to_cbz(
                item.source,
                destination,
                set_fields=set_fields,
                clear_fields=clear_fields,
                limits=_archive_limits(item),
            )
        if item.source_format == "pdf":
            if clear_fields:
                raise ApplyRefused(
                    "PDF metadata clearing is not supported by "
                    "the current immutable writer contract"
                )

            return write_pdf_metadata(
                item.source,
                destination,
                fields=set_fields,
            )
        raise ApplyRefused(f"unsupported planned source format: {item.source_format}")

    def verify(self, item: PreparedItem, candidate: Path) -> VerificationResult:
        owned = item.document["ownership_manifest"]
        set_fields = owned.get("set", {})
        clear_fields = tuple(str(value) for value in owned.get("clear", []))
        if item.source_format == "epub":
            calibre = {key: value for key, value in set_fields.items() if key in CALIBRE_FIELDS}
            exact_date = str(set_fields["date"]) if set_fields.get("date") else None
            return verify_epub(
                item.source, candidate, calibre, exact_date, _epub_roles(item.document)
            )
        if item.source_format in {"cbz", "cbr"}:
            source = item.source
            if item.source_format == "cbr":
                return _verify_repacked_cbr(source, candidate, set_fields, clear_fields)
            return verify_cbz(source, candidate, set_fields, clear_fields)
        if item.source_format == "pdf":
            if clear_fields:
                return VerificationResult(
                    False,
                    (),
                    (
                        "PDF metadata clearing is unsupported by "
                        "the current writer contract",
                    ),
                )

            return verify_pdf(
                item.source,
                candidate,
                set_fields,
            )
        return VerificationResult(False, (), ("unsupported source format",))


class ApplyEngine:
    def __init__(
        self,
        config: AppConfig,
        *,
        filesystem: NoClobberFilesystem | None = None,
        writers: WriterDispatcher | None = None,
        fault: FaultHook = _no_fault,
        disk_usage: Callable[[Path], DiskSpace] = _disk_usage,
    ) -> None:
        if config.database_path is None:
            raise ValueError("apply requires a configured database path")
        self.config = config
        self.database_path = config.database_path
        self.filesystem = filesystem or LinuxFilesystem()
        self.writers = writers or WriterDispatcher()
        self.fault = fault
        self.disk_usage = disk_usage

    def apply(self, plan_id: int) -> ApplySummary:
        with ProcessLock(lock_path(self.database_path)):
            migrate(self.database_path)
            connection = connect(self.database_path)
            try:
                return self._apply_locked(connection, plan_id)
            finally:
                connection.close()

    def preview(self, plan_id: int) -> ApplyPreview:
        migrate(self.database_path)
        with connect(self.database_path) as connection:
            plan, document = self._eligible_plan(connection, plan_id)
            self._require_current_apply_schema(document)
            prepared = self._prepare_items(document, run_id="preview")
        lifecycles: dict[str, int] = {}
        for item in prepared:
            lifecycles[item.lifecycle_policy] = lifecycles.get(item.lifecycle_policy, 0) + 1
        return ApplyPreview(
            plan_id=plan.id,
            digest=plan.sha256,
            item_count=len(prepared),
            destination_libraries=tuple(
                sorted({str(self._root_for(item)) for item in prepared})
            ),
            metadata_write_count=sum(
                1 for item in prepared if item.document.get("ownership_manifest", {}).get("set")
            ),
            cbr_to_cbz_count=sum(1 for item in prepared if item.source_format == "cbr"),
            lifecycle_counts=lifecycles,
            conflict_count=len(document.get("conflicts", []))
            + sum(len(item.document.get("conflicts", [])) for item in prepared),
            estimated_temporary_bytes=sum(_estimated_space(item) for item in prepared),
        )

    def _apply_locked(self, connection: sqlite3.Connection, plan_id: int) -> ApplySummary:
        journal = JournalRepository(connection)
        plan, document = self._eligible_plan(connection, plan_id)
        self._require_current_apply_schema(document)
        previous = journal.latest_for_plan(plan_id)
        if previous is not None:
            if previous.status is RunState.COMPLETE:
                raise PlanAlreadyComplete(f"plan {plan_id} is already complete")
            raise RecoveryRequired(
                f"plan {plan_id} has apply run {previous.id} in {previous.status.value}; recover it"
            )
        prepared = self._prepare_items(document, run_id="preflight")
        run = journal.create_run(plan_id, plan.sha256, [_journal_seed(item) for item in prepared])
        errors = self._preflight_all(prepared)
        if errors:
            for item in prepared:
                messages = errors.get(item.item_id)
                if messages:
                    journal.transition(
                        run.id,
                        item.item_id,
                        ItemState.STALE,
                        detail={"preflight_errors": messages},
                        fields={"error": "; ".join(messages)},
                    )
            journal.set_run_state(run.id, RunState.FAILED, error="plan preflight failed")
            raise StalePlan(_format_preflight_errors(errors))
        for item in prepared:
            journal.transition(run.id, item.item_id, ItemState.PREFLIGHT_OK)
        journal.set_run_state(run.id, RunState.RUNNING)
        prepared = self._prepare_items(document, run_id=run.id)
        for item in prepared:
            try:
                self._execute_item(journal, run.id, item)
            except DestinationExists as exc:
                self._mark_collision(journal, run.id, item.item_id, str(exc))
            except StalePlan as exc:
                self._mark_stale(journal, run.id, item.item_id, str(exc))
            except (OSError, ValueError, ApplyRefused, subprocess.SubprocessError) as exc:
                self._mark_failure(journal, run.id, item.item_id, exc)
        return self._finalize(journal, run.id)

    def recover(self, plan_id: int) -> ApplySummary:
        with ProcessLock(lock_path(self.database_path)):
            migrate(self.database_path)
            connection = connect(self.database_path)
            try:
                return self._recover_locked(connection, plan_id)
            finally:
                connection.close()

    def _recover_locked(self, connection: sqlite3.Connection, plan_id: int) -> ApplySummary:
        journal = JournalRepository(connection)
        plan, document = self._eligible_plan(connection, plan_id)
        self._require_current_apply_schema(document)
        run = journal.latest_for_plan(plan_id)
        if run is None:
            raise ApplyRefused(f"plan {plan_id} has no apply run to recover")
        if run.plan_digest != plan.sha256:
            raise ApplyRefused("apply run digest does not match the approved immutable plan")
        if run.status is RunState.COMPLETE:
            return _summary(journal, run)
        if any(item.state is ItemState.STALE for item in journal.items(run.id)):
            raise ApplyRefused("apply run contains stale actions; create a new plan")
        prepared = {item.item_id: item for item in self._prepare_items(document, run_id=run.id)}
        journal.set_run_state(run.id, RunState.RUNNING)
        for recorded in journal.items(run.id):
            try:
                self._recovery_preflight(recorded, prepared[recorded.item_id])
                self._recover_item(journal, recorded, prepared[recorded.item_id])
            except DestinationExists as exc:
                self._mark_collision(journal, run.id, recorded.item_id, str(exc))
            except StalePlan as exc:
                self._mark_stale(journal, run.id, recorded.item_id, str(exc))
            except (OSError, ValueError, ApplyRefused, subprocess.SubprocessError) as exc:
                self._mark_failure(journal, run.id, recorded.item_id, exc)
        return self._finalize(journal, run.id)

    def abandon(self, plan_id: int, *, reason: str) -> ApplySummary:
        """Close a safely abandonable apply run without touching media."""
        reason = reason.strip()
        if not reason:
            raise ApplyRefused("an abandonment reason is required")

        with ProcessLock(lock_path(self.database_path)):
            migrate(self.database_path)
            connection = connect(self.database_path)
            try:
                journal = JournalRepository(connection)
                plan = PlanStore(connection).get(plan_id)
                run = journal.latest_for_plan(plan_id)

                if run is None:
                    raise ApplyRefused(f"plan {plan_id} has no apply run to abandon")
                if run.plan_digest != plan.sha256:
                    raise ApplyRefused(
                        "apply run digest does not match the immutable plan"
                    )
                if run.status is RunState.COMPLETE:
                    raise ApplyRefused(
                        f"plan {plan_id} is already complete and cannot be abandoned"
                    )

                safe_states = {
                    ItemState.PENDING,
                    ItemState.PREFLIGHT_OK,
                    ItemState.FAILED,
                    ItemState.STALE,
                    ItemState.COMPLETE,
                }
                unsafe = [
                    item
                    for item in journal.items(run.id)
                    if item.state not in safe_states
                ]
                if unsafe:
                    rendered = ", ".join(
                        f"{item.item_id}={item.state.value}"
                        for item in unsafe
                    )
                    raise ApplyRefused(
                        "run cannot be abandoned while filesystem reconciliation "
                        f"may still be required: {rendered}; inspect recovery details first"
                    )

                message = f"abandoned by user: {reason}"

                connection.execute(
                    "INSERT OR IGNORE INTO plan_invalidations"
                    "(plan_id, reason, invalidated_at) "
                    "VALUES (?, ?, datetime('now'))",
                    (plan_id, message),
                )
                connection.commit()

                closed = journal.set_run_state(
                    run.id,
                    RunState.FAILED,
                    error=message,
                )
                return _summary(journal, closed)
            finally:
                connection.close()

    def status(self, plan_id: int) -> ApplySummary | None:
        migrate(self.database_path)
        with connect(self.database_path) as connection:
            journal = JournalRepository(connection)
            run = journal.latest_for_plan(plan_id)
            return _summary(journal, run) if run else None

    def inspect_recovery(self, plan_id: int) -> tuple[RecoveryInspection, ...]:
        migrate(self.database_path)
        with connect(self.database_path) as connection:
            journal = JournalRepository(connection)
            run = journal.latest_for_plan(plan_id)
            if run is None:
                return ()

            inspections = tuple(
                _inspect_item(item)
                for item in journal.items(run.id)
            )

            invalidated = connection.execute(
                "SELECT reason FROM plan_invalidations WHERE plan_id=?",
                (plan_id,),
            ).fetchone()

            if invalidated is not None:
                reason = str(invalidated["reason"])
                return tuple(
                    replace(
                        item,
                        proposed_action=f"none; plan is invalidated ({reason})",
                        manual_intervention=False,
                    )
                    for item in inspections
                )

            return inspections

    def _eligible_plan(
        self, connection: sqlite3.Connection, plan_id: int
    ) -> tuple[StoredPlan, dict[str, Any]]:
        try:
            plan = PlanStore(connection).get(plan_id)
        except KeyError as exc:
            raise ApplyRefused(f"plan {plan_id} does not exist") from exc
        except (RuntimeError, ValueError) as exc:
            raise ApplyRefused(f"authoritative plan validation failed: {exc}") from exc
        if plan.status != "approved" or plan.approval_digest != plan.sha256:
            raise ApplyRefused("plan is not explicitly approved for its exact digest")
        if plan.schema_version not in SUPPORTED_PLAN_SCHEMA_VERSIONS:
            raise ApplyRefused(f"unsupported plan schema version: {plan.schema_version}")
        invalidated = connection.execute(
            "SELECT reason FROM plan_invalidations WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if invalidated:
            raise ApplyRefused(f"plan is invalidated: {invalidated[0]}")
        superseded = connection.execute(
            "SELECT new_plan_id FROM plan_supersessions WHERE old_plan_id=?", (plan_id,)
        ).fetchone()
        if superseded:
            raise ApplyRefused(f"plan is superseded by plan {superseded[0]}")
        stale_decision = connection.execute(
            "SELECT item_id FROM plan_preconditions p WHERE p.plan_id=? AND "
            "p.decision_head_id<>(SELECT max(d.id) FROM decisions d "
            "WHERE d.source_fingerprint=p.source_fingerprint) LIMIT 1",
            (plan_id,),
        ).fetchone()
        if stale_decision:
            raise ApplyRefused(
                f"plan identity precondition is stale for item {stale_decision[0]}; "
                "create a new plan"
            )
        stale_run = connection.execute(
            "SELECT item_id FROM plan_preconditions p WHERE p.plan_id=? "
            "AND p.run_group_key IS NOT NULL AND p.run_group_decision_id IS NOT "
            "(SELECT max(r.id) FROM run_group_decisions r WHERE r.group_key=p.run_group_key "
            "AND r.provider='comic_vine') LIMIT 1",
            (plan_id,),
        ).fetchone()
        if stale_run:
            raise ApplyRefused(
                f"plan run-group precondition is stale for item {stale_run[0]}; create a new plan"
            )
        document = validate_plan_payload(plan.canonical_json)
        try:
            require_current_planning_policy(document)
        except ValueError as exc:
            connection.execute(
                "INSERT OR IGNORE INTO plan_invalidations(plan_id, reason, invalidated_at) "
                "VALUES (?, ?, datetime('now'))",
                (plan_id, LEGACY_POLICY_MESSAGE),
            )
            connection.commit()
            raise ApplyRefused(LEGACY_POLICY_MESSAGE) from exc
        if document.get("conflicts"):
            raise ApplyRefused("plan has unresolved plan-level conflicts")
        if any(item.get("blocked") or item.get("conflicts") for item in document["items"]):
            raise ApplyRefused("plan contains blocked or conflicting actions")
        if not document["items"]:
            raise ApplyRefused("plan has no actionable items")
        return plan, document

    def _require_current_apply_schema(self, document: dict[str, Any]) -> None:
        if document.get("schema_version") != PLAN_SCHEMA_VERSION:
            raise ApplyRefused(
                "plan predates immutable content-signature preconditions; "
                "abandon/recreate and approve a new plan before applying"
            )

    def _prepare_items(self, document: dict[str, Any], *, run_id: str) -> tuple[PreparedItem, ...]:
        output: list[PreparedItem] = []
        for item in document["items"]:
            canonical = item["canonical"]
            root, destination = self._immutable_destination(item, str(canonical["media_kind"]))
            policy, archive = _lifecycle(item)
            file_mode, directory_mode = _publication_modes(item)
            staging_directory = (
                root / ".kavita-ingest-staging" / run_id / _safe_item_id(item["item_id"])
            )
            source = Path(item["source"]["path"]).expanduser()
            output.append(
                PreparedItem(
                    str(item["item_id"]),
                    item,
                    source,
                    str(item["source"]["sha256"]),
                    int(item["source"]["size"]),
                    str(item["source"]["media_format"]),
                    str(item["source"].get("signature", "")),
                    destination,
                    policy,
                    archive,
                    staging_directory,
                    file_mode,
                    directory_mode,
                )
            )
        return tuple(output)

    def _immutable_destination(self, item: dict[str, Any], media_kind: str) -> tuple[Path, Path]:
        projection = item.get("kavita_projection")
        if not isinstance(projection, dict):
            raise ApplyRefused("plan item has no Kavita projection")
        root_value = projection.get("library_root")
        destination_value = projection.get("absolute_destination")
        if not isinstance(root_value, str) or not isinstance(destination_value, str):
            raise ApplyRefused(
                "plan predates absolute destination snapshots; create and approve a new plan"
            )
        root = Path(root_value).expanduser()
        destination = Path(destination_value).expanduser()
        if not root.is_absolute() or not destination.is_absolute():
            raise ApplyRefused("immutable library root and destination must be absolute")
        root = root.resolve(strict=False)
        destination = destination.resolve(strict=False)
        try:
            relative = destination.relative_to(root)
        except ValueError as exc:
            raise ApplyRefused("immutable destination escapes its planned library root") from exc
        if PurePosixPath(relative.as_posix()) != _safe_relative_destination(item):
            raise ApplyRefused("absolute and relative destination snapshots disagree")
        configured = self.config.books_root if media_kind == "book" else self.config.comics_root
        if configured is None or configured.expanduser().resolve(strict=False) != root:
            raise ApplyRefused("configured library root differs from approved plan snapshot")
        return root, destination

    def _preflight_all(self, items: tuple[PreparedItem, ...]) -> dict[str, list[str]]:
        errors: dict[str, list[str]] = {}
        for item in items:
            found: list[str] = []
            try:
                self._preflight_item(item)
            except (
                OSError,
                ValueError,
                ApplyRefused,
                zipfile.BadZipFile,
                rarfile.Error,
                pikepdf.PdfError,
                pikepdf.PasswordError,
            ) as exc:
                found.append(str(exc))
            if found:
                errors[item.item_id] = found
        by_device: dict[int, list[PreparedItem]] = {}
        for item in items:
            if item.item_id not in errors:
                by_device.setdefault(self._root_for(item).stat().st_dev, []).append(item)
        for grouped in by_device.values():
            probe = self.filesystem.probe_no_clobber(self._root_for(grouped[0]))
            if not probe.supported:
                for item in grouped:
                    errors.setdefault(item.item_id, []).append(probe.detail)
                continue
            required = sum(_estimated_space(item) for item in grouped)
            free = self.disk_usage(self._root_for(grouped[0])).free
            if free >= required:
                continue
            message = (
                "insufficient destination space for plan actions on shared filesystem: "
                f"need {required}, have {free}"
            )
            for item in grouped:
                errors.setdefault(item.item_id, []).append(message)
        return errors

    def _preflight_item(self, item: PreparedItem) -> None:
        if not item.source.is_file():
            raise StalePlan(f"planned source is missing: {item.source}")
        stat = item.source.stat()
        if stat.st_size != item.source_size:
            raise StalePlan(f"planned source size changed: {item.source}")
        if sha256_file(item.source) != item.source_hash:
            raise StalePlan(f"planned source fingerprint changed: {item.source}")
        root = self._root_for(item)
        if not root.is_dir():
            raise ApplyRefused(f"destination root is unavailable: {root}")
        _require_usable_parent(item.destination)
        if item.destination.exists():
            raise StalePlan(f"planned destination now exists: {item.destination}")
        if item.archive_path:
            if not item.archive_path.is_absolute():
                raise ApplyRefused("planned archive path must be absolute")
            _require_usable_parent(item.archive_path)
            if item.archive_path.exists():
                raise StalePlan(f"planned archive destination now exists: {item.archive_path}")
        self._check_capabilities(item)
        self._check_source_format(item)

    def _check_capabilities(self, item: PreparedItem) -> None:
        requirements = item.document.get("writer_versions", {})
        supported = {
            "kavita_ingest": importlib.metadata.version("kavita-ingest"),
            "comicinfo_schema": "2.1",
            "opf_patcher": "1",
            "pikepdf": importlib.metadata.version("pikepdf"),
            "rarfile": importlib.metadata.version("rarfile"),
        }
        required_keys = {
            "epub": {"ebook-meta", "opf_patcher"},
            "cbz": {"comicinfo_schema"},
            "cbr": {"comicinfo_schema", "rarfile", "unrar"},
            "pdf": {"ebook-meta", "pikepdf"},
        }.get(item.source_format)
        if required_keys is None:
            raise ApplyRefused(f"unsupported planned source format: {item.source_format}")
        missing = required_keys - set(requirements)
        if missing:
            raise ApplyRefused(f"plan lacks required writer versions: {sorted(missing)}")
        for key, expected in requirements.items():
            if key == "ebook-meta":
                try:
                    actual = require_safe_calibre_executable(
                        "ebook-meta"
                    )
                except ValueError as exc:
                    raise ApplyRefused(
                        str(exc)
                    ) from exc
            elif key == "unrar":
                actual = _tool_version("unrar", "UNRAR")
            elif key in supported:
                actual = supported[key]
            else:
                raise ApplyRefused(f"unknown writer capability in plan: {key}")
            if not _version_satisfies(str(expected), actual):
                raise ApplyRefused(
                    f"writer capability mismatch for {key}: plan={expected}, current={actual}"
                )

    def _check_source_format(self, item: PreparedItem) -> None:
        if item.source_format not in {"epub", "cbz", "cbr", "pdf"}:
            raise ApplyRefused(f"unsupported planned source format: {item.source_format}")
        signature, detected = detect_signature(item.source)
        if not item.source_signature:
            raise ApplyRefused(
                "plan lacks immutable source-signature evidence; regenerate the plan"
            )
        if signature != item.source_signature or detected.value != item.source_format:
            raise StalePlan(
                "source container signature no longer matches the approved plan"
            )
        if item.source_format == "epub":
            with zipfile.ZipFile(item.source) as archive:
                if archive.testzip() is not None or "mimetype" not in archive.namelist():
                    raise ApplyRefused("EPUB failed ZIP/mimetype capability preflight")
        elif item.source_format == "cbz":
            with zipfile.ZipFile(item.source) as archive:
                if archive.testzip() is not None:
                    raise ApplyRefused("CBZ failed CRC preflight")
        elif item.source_format == "cbr":
            with rarfile.RarFile(item.source) as archive:
                if archive.needs_password():
                    raise ApplyRefused("encrypted CBR is unsupported")
                if archive.volumelist() != [str(item.source)]:
                    raise ApplyRefused("multi-volume CBR is unsupported")
                members = archive.infolist()
                validate_inventory(
                    members,
                    _archive_limits(item),
                    link_names={member.filename for member in members if member.is_symlink()},
                    encrypted_names={
                        member.filename for member in members if member.needs_password()
                    },
                )
        elif item.source_format == "pdf":
            require_pdf_write_eligible(item.source)

    def _execute_item(self, journal: JournalRepository, run_id: str, item: PreparedItem) -> None:
        self.fault("before_staging", item.item_id)
        self._revalidate_source(item)
        self.filesystem.ensure_directory(
            self._root_for(item), item.staging_directory, item.created_directory_mode
        )
        staging = item.staging_directory / f"output-{os.urandom(6).hex()}{item.destination.suffix}"
        journal.transition(
            run_id, item.item_id, ItemState.STAGING, fields={"staging_path": str(staging)}
        )
        self.fault("during_writer", item.item_id)
        result = self.writers.stage(item, staging)
        result.require_valid()
        self.fault("after_staged_write", item.item_id)
        journal.transition(run_id, item.item_id, ItemState.STAGED)
        self.fault("after_staged", item.item_id)
        self._verify_and_commit(journal, run_id, item, staging)

    def _verify_and_commit(
        self, journal: JournalRepository, run_id: str, item: PreparedItem, staging: Path
    ) -> None:
        verified = self.writers.verify(item, staging)
        verified.require_valid()
        self.fault("after_staging_verification", item.item_id)
        self.filesystem.set_file_mode(staging, item.published_file_mode)
        self.filesystem.make_file_durable(staging)
        evidence = _output_evidence(staging, verified)
        journal.transition(
            run_id,
            item.item_id,
            ItemState.VERIFIED,
            detail=evidence,
            fields={
                "staged_hash": evidence["sha256"],
                "staged_size": evidence["size"],
                "verification_json": json.dumps(evidence, sort_keys=True),
            },
        )
        # VERIFIED seals the staged inode: every later operation is read-only or unlink-only.
        self.fault("after_verified_journal", item.item_id)
        self._commit_verified(journal, run_id, item, staging, evidence)

    def _commit_verified(
        self,
        journal: JournalRepository,
        run_id: str,
        item: PreparedItem,
        staging: Path,
        evidence: dict[str, Any],
    ) -> None:
        self._revalidate_source(item)
        if item.destination.exists():
            raise DestinationExists(f"destination appeared before commit: {item.destination}")
        self.filesystem.ensure_directory(
            self._root_for(item), item.destination.parent, item.created_directory_mode
        )
        journal.transition(run_id, item.item_id, ItemState.COMMITTING)
        self.fault("during_destination_commit", item.item_id)
        self.filesystem.commit(staging, item.destination)
        self.fault("after_destination_commit", item.item_id)
        self._verify_committed(item, evidence)
        journal.transition(
            run_id,
            item.item_id,
            ItemState.COMMITTED,
            fields={"destination_hash": evidence["sha256"]},
        )
        self.fault("after_committed", item.item_id)
        self._finish_lifecycle(journal, run_id, item)

    def _verify_committed(self, item: PreparedItem, evidence: Mapping[str, Any]) -> None:
        if not item.destination.is_file():
            raise OSError("committed destination is missing")
        if item.destination.stat().st_size != evidence["size"]:
            raise OSError("committed destination size differs from verified staging output")
        if sha256_file(item.destination) != evidence["sha256"]:
            raise OSError("committed destination hash differs from verified staging output")
        if (
            os.name == "posix"
            and (item.destination.stat().st_mode & 0o777) != item.published_file_mode
        ):
            raise OSError("committed destination permissions differ from immutable plan")
        result = self.writers.verify(item, item.destination)
        result.require_valid()

    def _finish_lifecycle(
        self, journal: JournalRepository, run_id: str, item: PreparedItem
    ) -> None:
        if item.lifecycle_policy == "preserve":
            journal.transition(run_id, item.item_id, ItemState.COMPLETE)
            _remove_empty_staging(item.staging_directory)
            return
        journal.transition(run_id, item.item_id, ItemState.CLEANUP_PENDING)
        self.fault("before_cleanup", item.item_id)
        self._revalidate_source(item)
        self.fault("during_cleanup", item.item_id)
        cleanup_path: str | None = None
        if item.lifecycle_policy == "archive_after_verify":
            if item.archive_path is None:
                raise ApplyRefused("archive lifecycle has no immutable archive path")
            self.filesystem.copy_for_archive(
                item.source,
                item.archive_path,
                item.source_hash,
                item.created_directory_mode,
            )
            if sha256_file(item.archive_path) != item.source_hash:
                raise OSError("committed archive does not match planned source")
            cleanup_path = str(item.archive_path)
        self.filesystem.durable_unlink(item.source)
        self.fault("after_cleanup_filesystem", item.item_id)
        journal.transition(
            run_id,
            item.item_id,
            ItemState.CLEANED,
            fields={"cleanup_path": cleanup_path},
        )
        journal.transition(run_id, item.item_id, ItemState.COMPLETE)
        _remove_empty_staging(item.staging_directory)

    def _recover_item(
        self, journal: JournalRepository, recorded: ApplyItem, item: PreparedItem
    ) -> None:
        state = recorded.state
        if state is ItemState.COMPLETE:
            return
        if state in {ItemState.STALE, ItemState.RECOVERY_REQUIRED}:
            return
        if state is ItemState.CLEANED:
            journal.transition(recorded.run_id, item.item_id, ItemState.COMPLETE)
            return
        if state in {ItemState.COMMITTED, ItemState.CLEANUP_PENDING}:
            self._recover_cleanup(journal, recorded, item)
            return
        if state in {ItemState.VERIFIED, ItemState.COMMITTING}:
            self._recover_verified(journal, recorded, item)
            return
        if state is ItemState.STAGED:
            staging = _required_staging(recorded)
            if not staging.is_file():
                self._manual_recovery(journal, recorded, "journalled staged output is missing")
                return
            self._verify_and_commit(journal, recorded.run_id, item, staging)
            return
        if state is ItemState.STAGING:
            journal.transition(
                recorded.run_id,
                item.item_id,
                ItemState.FAILED,
                detail={"reason": "interrupted staging retained; retrying to a new file"},
                fields={"error": "interrupted during staging"},
            )
        if item.destination.exists():
            self._manual_recovery(
                journal,
                journal.get_item(recorded.run_id, item.item_id),
                "destination exists without a durable verified/committing state",
            )
            return
        self._revalidate_source(item)
        self._execute_from_failed_or_preflight(journal, recorded.run_id, item)

    def _execute_from_failed_or_preflight(
        self, journal: JournalRepository, run_id: str, item: PreparedItem
    ) -> None:
        current = journal.get_item(run_id, item.item_id)
        if current.state not in {ItemState.FAILED, ItemState.PREFLIGHT_OK}:
            raise ApplyRefused(f"cannot retry item from {current.state.value}")
        self.filesystem.ensure_directory(
            self._root_for(item), item.staging_directory, item.created_directory_mode
        )
        staging = (
            item.staging_directory / f"recovery-{os.urandom(6).hex()}{item.destination.suffix}"
        )
        journal.transition(
            run_id, item.item_id, ItemState.STAGING, fields={"staging_path": str(staging)}
        )
        result = self.writers.stage(item, staging)
        result.require_valid()
        journal.transition(run_id, item.item_id, ItemState.STAGED)
        self._verify_and_commit(journal, run_id, item, staging)

    def _recover_verified(
        self, journal: JournalRepository, recorded: ApplyItem, item: PreparedItem
    ) -> None:
        if not recorded.staged_hash or recorded.staged_size is None:
            self._manual_recovery(journal, recorded, "verified state lacks staged evidence")
            return
        evidence = recorded.verification
        if item.destination.exists():
            if recorded.state is not ItemState.COMMITTING:
                self._manual_recovery(
                    journal,
                    recorded,
                    "destination exists but journal does not prove commit was attempted",
                )
                return
            if sha256_file(item.destination) != recorded.staged_hash:
                self._manual_recovery(
                    journal, recorded, "destination differs from durably verified output"
                )
                return
            self._revalidate_source(item)
            self._verify_committed(item, evidence)
            journal.transition(
                recorded.run_id,
                item.item_id,
                ItemState.COMMITTED,
                detail={"recovered": "recognized crash-after-filesystem-commit"},
                fields={"destination_hash": recorded.staged_hash},
            )
            self._remove_surviving_stage(recorded, item)
            self._finish_lifecycle(journal, recorded.run_id, item)
            return
        staging = _required_staging(recorded)
        if not staging.is_file() or sha256_file(staging) != recorded.staged_hash:
            self._manual_recovery(
                journal, recorded, "verified staging output is missing or changed"
            )
            return
        self._revalidate_source(item)
        if recorded.state is ItemState.VERIFIED:
            self._commit_verified(journal, recorded.run_id, item, staging, evidence)
        else:
            self.filesystem.commit(staging, item.destination)
            self._verify_committed(item, evidence)
            journal.transition(
                recorded.run_id,
                item.item_id,
                ItemState.COMMITTED,
                fields={"destination_hash": recorded.staged_hash},
            )
            self._finish_lifecycle(journal, recorded.run_id, item)

    def _recover_cleanup(
        self, journal: JournalRepository, recorded: ApplyItem, item: PreparedItem
    ) -> None:
        expected = recorded.destination_hash or recorded.staged_hash
        if (
            not expected
            or not item.destination.is_file()
            or sha256_file(item.destination) != expected
        ):
            self._manual_recovery(journal, recorded, "committed destination is missing or changed")
            return
        self._remove_surviving_stage(recorded, item)
        if recorded.state is ItemState.COMMITTED:
            if not item.source.is_file() or sha256_file(item.source) != item.source_hash:
                self._manual_recovery(
                    journal,
                    recorded,
                    "source is missing/changed before cleanup-pending was durably recorded",
                )
                return
            self._finish_lifecycle(journal, recorded.run_id, item)
            return
        if item.lifecycle_policy == "move_after_verify":
            if item.source.exists():
                self._revalidate_source(item)
                self.filesystem.durable_unlink(item.source)
            journal.transition(recorded.run_id, item.item_id, ItemState.CLEANED)
            journal.transition(recorded.run_id, item.item_id, ItemState.COMPLETE)
            return
        if item.lifecycle_policy == "archive_after_verify":
            self._recover_archive_cleanup(journal, recorded, item)
            return
        self._manual_recovery(journal, recorded, "preserve lifecycle entered cleanup state")

    def _remove_surviving_stage(self, recorded: ApplyItem, item: PreparedItem) -> None:
        if not recorded.staging_path:
            return
        staging = Path(recorded.staging_path)
        if not staging.exists():
            return
        try:
            staging.relative_to(item.staging_directory)
        except ValueError as exc:
            raise RecoveryRequired("recorded staging path escapes its action directory") from exc
        if not staging.is_file() or sha256_file(staging) != recorded.staged_hash:
            raise RecoveryRequired("surviving published staging path is not the verified output")
        if staging.stat().st_ino != item.destination.stat().st_ino:
            raise RecoveryRequired("surviving staging path is not the published destination inode")
        self.filesystem.durable_unlink(staging)

    def _recover_archive_cleanup(
        self, journal: JournalRepository, recorded: ApplyItem, item: PreparedItem
    ) -> None:
        archive = item.archive_path
        if archive is None:
            self._manual_recovery(journal, recorded, "archive path is missing from plan")
            return
        archive_valid = archive.is_file() and sha256_file(archive) == item.source_hash
        if archive.exists() and not archive_valid:
            self._manual_recovery(journal, recorded, "archive destination exists with wrong hash")
            return
        if item.source.exists():
            self._revalidate_source(item)
            if not archive_valid:
                self.filesystem.copy_for_archive(
                    item.source, archive, item.source_hash, item.created_directory_mode
                )
            self.filesystem.durable_unlink(item.source)
        elif not archive_valid:
            self._manual_recovery(
                journal, recorded, "both planned source and verified archive are missing"
            )
            return
        journal.transition(
            recorded.run_id,
            item.item_id,
            ItemState.CLEANED,
            fields={"cleanup_path": str(archive)},
        )
        journal.transition(recorded.run_id, item.item_id, ItemState.COMPLETE)

    def _manual_recovery(
        self, journal: JournalRepository, recorded: ApplyItem, explanation: str
    ) -> None:
        if recorded.state is ItemState.RECOVERY_REQUIRED:
            return
        journal.transition(
            recorded.run_id,
            recorded.item_id,
            ItemState.RECOVERY_REQUIRED,
            detail={"manual_intervention": explanation},
            fields={"recovery_detail": explanation, "error": explanation},
        )

    def _revalidate_source(self, item: PreparedItem) -> None:
        if not item.source.is_file():
            raise StalePlan(f"planned source disappeared: {item.source}")
        if sha256_file(item.source) != item.source_hash:
            raise StalePlan(f"planned source fingerprint changed: {item.source}")

    def _root_for(self, item: PreparedItem) -> Path:
        projection = item.document["kavita_projection"]
        return Path(projection["library_root"]).expanduser().resolve(strict=False)

    def _recovery_preflight(self, recorded: ApplyItem, item: PreparedItem) -> None:
        self._check_capabilities(item)
        root = self._root_for(item)
        if not root.is_dir():
            raise ApplyRefused(f"destination root is unavailable: {root}")
        before_commit = {
            ItemState.PENDING,
            ItemState.PREFLIGHT_OK,
            ItemState.STAGING,
            ItemState.STAGED,
            ItemState.FAILED,
            ItemState.VERIFIED,
        }
        if recorded.state in before_commit:
            self._revalidate_source(item)
        if recorded.state in before_commit - {ItemState.VERIFIED} and item.destination.exists():
            raise RecoveryRequired(
                f"destination exists without a durable commit attempt: {item.destination}"
            )

    def _mark_collision(
        self, journal: JournalRepository, run_id: str, item_id: str, message: str
    ) -> None:
        current = journal.get_item(run_id, item_id)
        if current.state is ItemState.COMMITTING:
            journal.transition(
                run_id,
                item_id,
                ItemState.STALE,
                detail={"destination_collision": message},
                fields={"error": message},
            )
        else:
            self._manual_recovery(journal, current, message)

    def _mark_stale(
        self, journal: JournalRepository, run_id: str, item_id: str, message: str
    ) -> None:
        current = journal.get_item(run_id, item_id)
        if current.state in {
            ItemState.PREFLIGHT_OK,
            ItemState.STAGING,
            ItemState.STAGED,
            ItemState.VERIFIED,
            ItemState.COMMITTING,
        }:
            journal.transition(
                run_id,
                item_id,
                ItemState.STALE,
                detail={"stale_source": message},
                fields={"error": message},
            )
        else:
            self._manual_recovery(journal, current, message)

    def _mark_failure(
        self, journal: JournalRepository, run_id: str, item_id: str, error: Exception
    ) -> None:
        current = journal.get_item(run_id, item_id)
        message = f"{type(error).__name__}: {error}"
        if current.state in {ItemState.STAGING, ItemState.STAGED}:
            journal.transition(
                run_id,
                item_id,
                ItemState.FAILED,
                detail={"error": message},
                fields={"error": message},
            )
        elif current.state in {
            ItemState.PENDING,
            ItemState.PREFLIGHT_OK,
            ItemState.VERIFIED,
            ItemState.COMMITTING,
            ItemState.COMMITTED,
            ItemState.CLEANUP_PENDING,
            ItemState.CLEANED,
        }:
            if current.state in {
                ItemState.COMMITTING,
                ItemState.COMMITTED,
                ItemState.CLEANUP_PENDING,
            }:
                journal.note_error(run_id, item_id, message)
            else:
                self._manual_recovery(journal, current, message)

    def _finalize(self, journal: JournalRepository, run_id: str) -> ApplySummary:
        items = journal.items(run_id)
        if all(item.state is ItemState.COMPLETE for item in items):
            run = journal.set_run_state(run_id, RunState.COMPLETE)
        elif any(
            item.state
            in {
                ItemState.RECOVERY_REQUIRED,
                ItemState.FAILED,
                ItemState.COMMITTING,
                ItemState.COMMITTED,
                ItemState.CLEANUP_PENDING,
            }
            for item in items
        ):
            run = journal.set_run_state(run_id, RunState.RECOVERY_REQUIRED)
        else:
            run = journal.set_run_state(run_id, RunState.FAILED)
        return _summary(journal, run)


def _journal_seed(item: PreparedItem) -> dict[str, str | None]:
    return {
        "item_id": item.item_id,
        "source_path": str(item.source),
        "planned_source_hash": item.source_hash,
        "destination_path": str(item.destination),
        "lifecycle_policy": item.lifecycle_policy,
        "archive_path": str(item.archive_path) if item.archive_path else None,
    }


def _safe_relative_destination(item: dict[str, Any]) -> PurePosixPath:
    projection = item.get("kavita_projection")
    if not isinstance(projection, dict) or not isinstance(projection.get("destination"), str):
        raise ApplyRefused(f"item {item.get('item_id')} has no immutable destination")
    path = PurePosixPath(projection["destination"])
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        raise ApplyRefused(f"unsafe planned destination: {path}")
    return path


def _safe_item_id(value: object) -> str:
    text = str(value)
    if not text or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for char in text
    ):
        raise ApplyRefused(f"unsafe plan item id for staging: {text!r}")
    return text


def _lifecycle(item: dict[str, Any]) -> tuple[str, Path | None]:
    actions = item.get("lifecycle_actions", [])
    if not isinstance(actions, list) or not actions:
        raise ApplyRefused("plan item has no source lifecycle action")
    final = actions[-1]
    action = final.get("action") if isinstance(final, dict) else None
    aliases = {
        "remove_source_after_verified_commit": "move_after_verify",
        "retain_source": "preserve",
    }
    policy = aliases.get(str(action), str(action))
    if policy not in {"move_after_verify", "preserve", "archive_after_verify"}:
        raise ApplyRefused(f"unsupported planned source lifecycle: {action}")
    archive = None
    if policy == "archive_after_verify":
        value = final.get("archive_path")
        if not isinstance(value, str) or not value:
            raise ApplyRefused("archive lifecycle lacks an immutable archive path")
        archive = Path(value).expanduser()
    return policy, archive


def _require_usable_parent(path: Path) -> None:
    current = path.parent
    while not current.exists() and current != current.parent:
        current = current.parent
    if not current.is_dir():
        raise ApplyRefused(f"destination parent has a non-directory component: {current}")
    if not os.access(current, os.W_OK | os.X_OK):
        raise ApplyRefused(f"destination parent is not writable: {current}")


def _estimated_space(item: PreparedItem) -> int:
    inventory_total = sum(
        int(entry.get("size", 0))
        for entry in item.document.get("expected_inventory", [])
        if isinstance(entry, dict)
    )
    if item.source_format == "cbr":
        estimate = max(item.source_size * 4, inventory_total * 2 + item.source_size)
    else:
        estimate = max(item.source_size * 2, inventory_total + item.source_size)
    return max(64 * 1024 * 1024, int(estimate * 1.25))


def _archive_limits(item: PreparedItem) -> ArchiveLimits:
    policy = item.document.get("planning_policy", {})
    values = policy.get("archive_limits", {}) if isinstance(policy, dict) else {}
    if not isinstance(values, dict) or not values:
        return ArchiveLimits()
    try:
        return ArchiveLimits(
            max_entries=int(values["max_entries"]),
            max_entry_bytes=int(values["max_entry_bytes"]),
            max_total_bytes=int(values["max_total_bytes"]),
            max_path_depth=int(values["max_path_depth"]),
            max_ratio=float(values["max_ratio"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApplyRefused("plan contains invalid immutable archive safety limits") from exc


def _publication_modes(item: dict[str, Any]) -> tuple[int, int]:
    policy = item.get("planning_policy", {})
    version = policy.get("version", 1) if isinstance(policy, dict) else 1
    permissions = policy.get("permissions", {}) if isinstance(policy, dict) else {}
    if version != 2:
        raise ApplyRefused(LEGACY_POLICY_MESSAGE)
    if not isinstance(permissions, dict):
        raise ApplyRefused("immutable planning policy lacks publication permissions")
    try:
        return int(str(permissions["file_mode"]), 8), int(
            str(permissions["directory_mode"]), 8
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApplyRefused("invalid immutable publication permissions") from exc


def _epub_roles(item: dict[str, Any]) -> dict[str, Sequence[str]]:
    contributors = item.get("canonical", {}).get("contributors", {})
    mapping = {"translators": "trl", "editors": "edt", "illustrators": "ill", "colorists": "clr"}
    return {
        role: tuple(str(name) for name in contributors.get(field, []))
        for field, role in mapping.items()
        if field in contributors
    }


def _verify_repacked_cbr(
    source: Path,
    candidate: Path,
    set_fields: dict[str, object],
    clear_fields: tuple[str, ...],
) -> VerificationResult:
    errors: list[str] = []
    result = verify_cbz(candidate, candidate, set_fields, clear_fields)
    errors.extend(result.errors)
    try:
        with rarfile.RarFile(source) as archive, zipfile.ZipFile(candidate) as target:
            target_payloads = {
                info.filename: target.read(info.filename)
                for info in target.infolist()
                if not info.is_dir() and info.filename.casefold() != "comicinfo.xml"
            }
            expected = {
                info.filename.replace("\\", "/"): archive.read(info)
                for info in archive.infolist()
                if not info.is_dir()
                and not info.is_symlink()
                and info.filename.casefold() != "comicinfo.xml"
            }
            if list(target_payloads) != list(expected) or target_payloads != expected:
                errors.append("CBR-to-CBZ payload bytes or ordering changed")
    except (OSError, rarfile.Error, zipfile.BadZipFile) as exc:
        errors.append(str(exc))
    return VerificationResult(not errors, result.checks + ("cbr_payload_inventory",), tuple(errors))


def _output_evidence(path: Path, verified: VerificationResult) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
        "checks": list(verified.checks),
    }
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            evidence["inventory"] = [
                {"name": info.filename, "size": info.file_size, "crc": info.CRC}
                for info in archive.infolist()
            ]
    elif path.suffix.casefold() == ".pdf":
        with pikepdf.open(path) as pdf:
            evidence["page_count"] = len(pdf.pages)
    return evidence


def _required_staging(item: ApplyItem) -> Path:
    if not item.staging_path:
        raise ApplyRefused("journal has no staging path")
    return Path(item.staging_path)


def _remove_empty_staging(path: Path) -> None:
    try:
        path.rmdir()
        path.parent.rmdir()
    except OSError:
        pass


def _tool_version(executable: str, marker: str) -> str:
    path = shutil.which(executable)
    if path is None:
        raise ApplyRefused(f"required helper is unavailable: {executable}")
    arguments = [path, "--version"] if executable == "ebook-meta" else [path, "-?"]
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=10, check=False)
    lines = [
        line.strip() for line in f"{result.stdout}\n{result.stderr}".splitlines() if line.strip()
    ]
    return next(
        (line for line in lines if marker.casefold() in line.casefold()),
        lines[0] if lines else path,
    )


def _version_satisfies(expected: str, actual: str) -> bool:
    if expected in actual:
        return True
    expected_match = re.search(r"\d+(?:\.\d+)+", expected)
    actual_match = re.search(r"\d+(?:\.\d+)+", actual)
    if expected_match is None or actual_match is None:
        return False
    expected_numbers = tuple(int(value) for value in expected_match.group().split("."))
    actual_numbers = tuple(int(value) for value in actual_match.group().split("."))
    while expected_numbers and expected_numbers[-1] == 0:
        expected_numbers = expected_numbers[:-1]
    while actual_numbers and actual_numbers[-1] == 0:
        actual_numbers = actual_numbers[:-1]
    return bool(expected_numbers) and expected_numbers == actual_numbers


def _format_preflight_errors(errors: Mapping[str, Sequence[str]]) -> str:
    return "; ".join(f"{item}: {', '.join(messages)}" for item, messages in errors.items())


def _summary(journal: JournalRepository, run: ApplyRun) -> ApplySummary:
    counts: dict[str, int] = {}
    for item in journal.items(run.id):
        counts[item.state.value] = counts.get(item.state.value, 0) + 1
    return ApplySummary(run.id, run.plan_id, run.status, counts)


def _inspect_item(item: ApplyItem) -> RecoveryInspection:
    source = Path(item.source_path)
    staging = Path(item.staging_path) if item.staging_path else None
    destination = Path(item.destination_path)
    source_matches = (
        sha256_file(source) == item.planned_source_hash if source.is_file() else None
    )
    staging_matches = (
        sha256_file(staging) == item.staged_hash
        if staging and staging.is_file() and item.staged_hash
        else None
    )
    expected_destination = item.destination_hash or item.staged_hash
    destination_matches = (
        sha256_file(destination) == expected_destination
        if destination.is_file() and expected_destination
        else None
    )
    proposed, manual = _proposed_recovery(item, destination.exists(), destination_matches)
    return RecoveryInspection(
        item.item_id,
        item.state,
        item.source_path,
        source.exists(),
        source_matches,
        item.staging_path,
        staging_matches,
        item.destination_path,
        destination.exists(),
        destination_matches,
        proposed,
        manual,
        item.recovery_detail or item.error,
    )


def _proposed_recovery(
    item: ApplyItem, destination_exists: bool, destination_matches: bool | None
) -> tuple[str, bool]:
    if item.state is ItemState.COMPLETE:
        return "none; item is complete", False
    if item.state is ItemState.COMMITTING and destination_exists and destination_matches:
        return "recognize verified destination commit, then resume lifecycle", False
    if item.state in {ItemState.VERIFIED, ItemState.COMMITTING} and not destination_exists:
        return "publish retained verified staging output with no-clobber commit", False
    if item.state in {ItemState.COMMITTED, ItemState.CLEANUP_PENDING} and destination_matches:
        return "resume or recognize planned source lifecycle", False
    if item.state in {
        ItemState.PREFLIGHT_OK,
        ItemState.STAGING,
        ItemState.STAGED,
        ItemState.FAILED,
    }:
        return "retry staging only if source and destination preconditions still hold", False
    return "manual intervention required; no safe automatic transition is proven", True
