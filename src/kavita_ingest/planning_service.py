from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pikepdf
import rarfile

from .calibre import require_safe_calibre_executable
from .canonical import ResolutionLevel
from .config import AppConfig
from .decisions import DecisionRepository, DecisionType
from .discovery import inspect_source
from .domain import MediaKind, SourceFormat, SourceRecord
from .planning import (
    PlanConflict,
    PlanDocument,
    PlanningPolicySnapshot,
    SourcePrecondition,
    build_snapshot,
    new_plan,
)
from .projection import project_book, project_comic, project_comic_pdf
from .resolution import resolve_explicit_identity
from .run_groups import RunGroupRepository, run_group_key


class NoActionableItems(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PlanningExclusion:
    path: str
    category: str
    explanation: str


@dataclass(frozen=True, slots=True)
class PlanBuildResult:
    document: PlanDocument
    accepted_included: int
    work_only_included: int
    manual_included: int
    unapproved_excluded: int
    unresolved_blocked: int
    skipped: int
    conflicts: int
    exclusions: tuple[PlanningExclusion, ...]


class PlanBuilder:
    """Build immutable plans from persisted reviewed state without provider access."""

    def __init__(self, connection: sqlite3.Connection, config: AppConfig) -> None:
        self.connection = connection
        self.config = config
        self.decisions = DecisionRepository(connection)
        self.run_groups = RunGroupRepository(connection)

    def build(self, root: Path, *, name: str | None = None) -> PlanBuildResult:
        scope = root.expanduser().resolve(strict=True)
        if not scope.is_dir():
            raise ValueError(f"planning scope is not a directory: {scope}")
        policy = self._policy()
        snapshots = []
        exclusions: list[PlanningExclusion] = []
        included = work_only = manual = skipped = blocked = unapproved = 0
        for row in self._reviewed_sources(scope):
            source = _source(row)
            latest = self.decisions.latest(source)
            if latest is None:
                unapproved += 1
                exclusions.append(
                    PlanningExclusion(str(source.path), "unapproved", "no explicit decision")
                )
                continue
            if latest.decision_type in {DecisionType.SKIPPED, DecisionType.UNRESOLVED}:
                skipped += 1
                exclusions.append(
                    PlanningExclusion(
                        str(source.path),
                        latest.decision_type.value,
                        "latest decision excludes item",
                    )
                )
                continue
            if latest.decision_type is DecisionType.REJECTED:
                unapproved += 1
                exclusions.append(
                    PlanningExclusion(str(source.path), "rejected", "latest candidate was rejected")
                )
                continue
            try:
                current = inspect_source(source.path)
            except OSError as exc:
                missing = not source.path.exists()
                if not missing:
                    blocked += 1
                exclusions.append(
                    PlanningExclusion(
                        str(source.path),
                        "historical_missing" if missing else "unreadable_source",
                        (
                            "historical reviewed source no longer exists; "
                            "ignored for this plan"
                            if missing
                            else f"reviewed source could not be inspected: {exc}"
                        ),
                    )
                )
                continue
            if (
                current.sha256 != source.sha256
                or current.size != source.size
                or current.format is not source.format
                or current.signature != source.signature
            ):
                blocked += 1
                exclusions.append(
                    PlanningExclusion(
                        str(source.path),
                        "stale_source",
                        "source changed after review; scan, review and plan again",
                    )
                )
                continue
            kind = MediaKind(str(row["kind"]))
            resolution = resolve_explicit_identity(self.decisions, source, kind)
            if (
                not resolution.eligible
                or resolution.identity is None
                or resolution.authorization is None
            ):
                blocked += 1
                exclusions.append(
                    PlanningExclusion(
                        str(source.path),
                        "blocked",
                        "; ".join(resolution.blocks) or "identity is not plan-eligible",
                    )
                )
                continue
            identity = resolution.identity
            run_decision_id: int | None = None
            run_key: str | None = None
            if identity.media_kind is MediaKind.COMIC and identity.series_title:
                run_key = run_group_key(identity.series_title)
                run_decision = self.run_groups.latest(run_key, "comic_vine")
                if run_decision is not None:
                    run_decision_id = run_decision.id
                    accepted_run = identity.provider_identity.get("run_id")
                    if (
                        run_decision.active
                        and accepted_run
                        and run_decision.provider_run_id != accepted_run
                    ):
                        blocked += 1
                        exclusions.append(
                            PlanningExclusion(
                                str(source.path),
                                "run_changed",
                                "selected comic run differs from the accepted issue identity",
                            )
                        )
                        continue
            try:
                projection, transformations, versions = self._project(
                    identity, current, policy
                )
            except ValueError as exc:
                blocked += 1
                exclusions.append(PlanningExclusion(str(source.path), "blocked", str(exc)))
                continue
            destination_root = self._destination_root(identity.media_kind)
            archive_path = self._archive_path(scope, current.path)
            provenance = {
                "explicit_approval": True,
                "decision_id": resolution.authorization.id,
                "decision_head_id": latest.id,
                "decision_type": resolution.authorization.decision_type.value,
                "run_group_key": run_key,
                "run_group_decision_id": run_decision_id,
                "planning_policy_digest": policy.digest(),
            }
            snapshot = build_snapshot(
                item_id=f"source-{int(row['id'])}-{current.sha256[:12]}",
                source=SourcePrecondition(
                    str(current.path),
                    current.sha256,
                    current.size,
                    current.mtime_ns,
                    current.format.value,
                    current.signature,
                ),
                identity=identity,
                projection=projection,
                decision_provenance=provenance,
                transformations=transformations,
                writer_versions=versions,
                expected_inventory=_expected_inventory(row),
                verification_requirements=_verification_requirements(current.format),
                lifecycle_policy=policy.source_lifecycle,
                archive_path=str(archive_path) if archive_path else None,
                destination_root=str(destination_root),
                planning_policy=policy,
            )
            item_conflicts = list(snapshot.conflicts)
            absolute_destination = destination_root / projection.destination
            if absolute_destination.exists():
                item_conflicts.append(
                    PlanConflict(
                        "destination_exists",
                        "planned destination already exists; no overwrite is permitted",
                        (str(absolute_destination),),
                    )
                )
            if archive_path and archive_path.exists():
                item_conflicts.append(
                    PlanConflict(
                        "archive_exists",
                        "planned archive destination already exists",
                        (str(archive_path),),
                    )
                )
            snapshots.append(replace(snapshot, conflicts=tuple(item_conflicts)))
            included += 1
            work_only += identity.resolution is ResolutionLevel.WORK_ONLY
            manual += resolution.authorization.decision_type is DecisionType.MANUAL_IDENTITY
        if not snapshots:
            detail = "; ".join(f"{item.path}: {item.explanation}" for item in exclusions[:5])
            raise NoActionableItems(f"no explicitly approved plan-eligible items; {detail}")
        document = new_plan(name or f"plan-{scope.name}", tuple(snapshots), policy)
        conflict_count = len(document.conflicts) + sum(len(item.conflicts) for item in snapshots)
        return PlanBuildResult(
            document,
            included,
            work_only,
            manual,
            unapproved,
            blocked,
            skipped,
            conflict_count,
            tuple(exclusions),
        )

    def _reviewed_sources(self, scope: Path) -> tuple[sqlite3.Row, ...]:
        rows = self.connection.execute(
            """
            SELECT s.*, c.kind, c.subtype, i.status AS inspection_status,
                   i.metadata_json, i.evidence_json
            FROM sources s
            JOIN classifications c ON c.id=(
                SELECT id FROM classifications WHERE source_id=s.id ORDER BY id DESC LIMIT 1
            )
            JOIN inspections i ON i.id=(
                SELECT id FROM inspections WHERE source_id=s.id ORDER BY id DESC LIMIT 1
            )
            WHERE i.status='ok'
            ORDER BY s.path
            """
        ).fetchall()
        return tuple(row for row in rows if _within_scope(Path(str(row["path"])), scope))

    def _policy(self) -> PlanningPolicySnapshot:
        archive_root = (
            str(self.config.source_archive_root.expanduser().resolve(strict=False))
            if self.config.source_archive_root
            else None
        )
        return PlanningPolicySnapshot(
            self.config.naming_policy(),
            self.config.source_lifecycle,
            archive_root,
            self.config.cbr_conversion_enabled,
            self.config.archive_limits(),
            self.config.published_file_mode,
            self.config.created_directory_mode,
        )

    def _project(
        self,
        identity: Any,
        source: SourceRecord,
        policy: PlanningPolicySnapshot,
    ) -> tuple[Any, tuple[dict[str, Any], ...], dict[str, str]]:
        source_format = source.format
        if source_format is SourceFormat.CBR:
            if not policy.cbr_conversion_enabled:
                raise ValueError(
                    "CBR conversion is disabled and safe in-place CBR metadata writing "
                    "is unsupported"
                )
            return (
                project_comic(identity, ".cbz", policy.naming),
                ({"type": "cbr_to_cbz"},),
                _writer_versions(source_format),
            )
        if source_format is SourceFormat.PDF and identity.media_kind is MediaKind.COMIC:
            return (
                project_comic_pdf(identity, policy.naming),
                ({"type": "pdf_comic_metadata"},),
                _writer_versions(source_format),
            )
        extension = f".{source_format.value}"
        projection = (
            project_book(identity, extension, policy.naming)
            if identity.media_kind is MediaKind.BOOK
            else project_comic(identity, extension, policy.naming)
        )
        transformation = (
            "zip_comic_to_cbz"
            if source_format is SourceFormat.CBZ and source.path.suffix.casefold() != ".cbz"
            else "metadata_only"
        )
        return projection, ({"type": transformation},), _writer_versions(source_format)

    def _destination_root(self, kind: MediaKind) -> Path:
        root = self.config.books_root if kind is MediaKind.BOOK else self.config.comics_root
        if root is None:
            raise ValueError(f"{kind.value} destination root is not configured")
        resolved = root.expanduser().resolve(strict=False)
        if not resolved.is_dir():
            raise ValueError(f"{kind.value} destination root is unavailable: {resolved}")
        return resolved

    def _archive_path(self, scope: Path, source: Path) -> Path | None:
        if self.config.source_lifecycle != "archive_after_verify":
            return None
        if self.config.source_archive_root is None:
            raise ValueError("archive_after_verify requires source.archive_root")
        relative = source.resolve(strict=True).relative_to(scope)
        return self.config.source_archive_root.expanduser().resolve(strict=False) / relative


def _source(row: sqlite3.Row) -> SourceRecord:
    return SourceRecord(
        Path(str(row["path"])),
        int(row["size"]),
        int(row["mtime_ns"]),
        str(row["sha256"]),
        SourceFormat(str(row["format"])),
        str(row["signature"]),
    )


def _within_scope(path: Path, scope: Path) -> bool:
    try:
        path.expanduser().resolve(strict=False).relative_to(scope)
    except ValueError:
        return False
    return True


def _expected_inventory(row: sqlite3.Row) -> tuple[dict[str, Any], ...]:
    metadata = json.loads(str(row["metadata_json"]))
    inventory = metadata.get("inventory", []) if isinstance(metadata, dict) else []
    output = [dict(item) for item in inventory if isinstance(item, dict)]
    if not output and isinstance(metadata, dict) and metadata.get("page_count") is not None:
        output.append(
            {
                "kind": "pdf_pages",
                "page_count": int(metadata["page_count"]),
                "content_stream_sha256": metadata.get("content_stream_sha256", []),
            }
        )
    return tuple(output)


def _verification_requirements(source_format: SourceFormat) -> tuple[str, ...]:
    common = ("source_sha256", "metadata_readback", "destination_verification")
    if source_format is SourceFormat.CBR:
        return common + ("archive_inventory", "payload_byte_preservation")
    if source_format in {SourceFormat.CBZ, SourceFormat.EPUB}:
        return common + ("archive_inventory",)
    if source_format is SourceFormat.PDF:
        return common + ("pdf_semantic_preservation",)
    return common


def _writer_versions(source_format: SourceFormat) -> dict[str, str]:
    if source_format is SourceFormat.EPUB:
        return {
            "ebook-meta": require_safe_calibre_executable(
                "ebook-meta"
            ),
            "opf_patcher": "1",
        }

    if source_format is SourceFormat.PDF:
        return {
            "ebook-meta": require_safe_calibre_executable(
                "ebook-meta"
            ),
            "pikepdf": pikepdf.__version__,
        }
    if source_format is SourceFormat.CBZ:
        return {"comicinfo_schema": "2.1"}
    if source_format is SourceFormat.CBR:
        return {
            "comicinfo_schema": "2.1",
            "rarfile": rarfile.__version__,
            "unrar": _tool_version("unrar", "UNRAR"),
        }
    raise ValueError(f"unsupported source format for planning: {source_format.value}")


def _tool_version(executable: str, marker: str) -> str:
    path = shutil.which(executable)
    if path is None:
        raise ValueError(f"required planning helper is unavailable: {executable}")
    arguments = [path, "--version"] if executable == "ebook-meta" else [path, "-?"]
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=10, check=False)
    lines = [
        line.strip() for line in f"{result.stdout}\n{result.stderr}".splitlines() if line.strip()
    ]
    value = next(
        (line for line in lines if marker.casefold() in line.casefold()),
        lines[0] if lines else path,
    )
    match = re.search(r"\d+(?:\.\d+)+", value)
    return match.group() if match else value
