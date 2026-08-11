from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .archive_safety import ArchiveLimits
from .canonical import CanonicalIdentity
from .naming import NamingPolicy
from .projection import KavitaProjection

PLAN_SCHEMA_VERSION = 2
SUPPORTED_PLAN_SCHEMA_VERSIONS = {1, PLAN_SCHEMA_VERSION}


@dataclass(frozen=True, slots=True)
class PlanningPolicySnapshot:
    naming: NamingPolicy
    source_lifecycle: str
    source_archive_root: str | None
    cbr_conversion_enabled: bool
    archive_limits: ArchiveLimits
    published_file_mode: int = 0o644
    created_directory_mode: int = 0o755
    version: int = 2
    projection_policy_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "projection_policy_version": self.projection_policy_version,
            "naming": self.naming.to_dict(),
            "source_lifecycle": self.source_lifecycle,
            "source_archive_root": self.source_archive_root,
            "cbr_conversion_enabled": self.cbr_conversion_enabled,
            "archive_limits": {
                "max_entries": self.archive_limits.max_entries,
                "max_entry_bytes": self.archive_limits.max_entry_bytes,
                "max_total_bytes": self.archive_limits.max_total_bytes,
                "max_path_depth": self.archive_limits.max_path_depth,
                "max_ratio": self.archive_limits.max_ratio,
            },
            "permissions": {
                "file_mode": f"{self.published_file_mode:04o}",
                "directory_mode": f"{self.created_directory_mode:04o}",
            },
        }

    def digest(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(payload).hexdigest()


def default_planning_policy() -> PlanningPolicySnapshot:
    return PlanningPolicySnapshot(NamingPolicy(), "move_after_verify", None, True, ArchiveLimits())


@dataclass(frozen=True, slots=True)
class SourcePrecondition:
    path: str
    sha256: str
    size: int
    mtime_ns: int
    media_format: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "media_format": self.media_format,
        }


@dataclass(frozen=True, slots=True)
class PlanConflict:
    code: str
    explanation: str
    paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "explanation": self.explanation, "paths": list(self.paths)}


@dataclass(frozen=True, slots=True)
class ResolvedItemSnapshot:
    item_id: str
    source: SourcePrecondition
    canonical: dict[str, Any]
    partial_resolution: dict[str, Any]
    provenance: dict[str, Any]
    kavita_projection: dict[str, Any] | None
    metadata_changes: dict[str, Any]
    ownership_manifest: dict[str, Any]
    transformations: tuple[dict[str, Any], ...]
    writer_versions: dict[str, str]
    expected_inventory: tuple[dict[str, Any], ...]
    verification_requirements: tuple[str, ...]
    lifecycle_actions: tuple[dict[str, Any], ...]
    planning_policy: dict[str, Any]
    conflicts: tuple[PlanConflict, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts) or self.kavita_projection is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_version": 1,
            "item_id": self.item_id,
            "source": self.source.to_dict(),
            "canonical": self.canonical,
            "partial_resolution": self.partial_resolution,
            "provenance": self.provenance,
            "kavita_projection": self.kavita_projection,
            "metadata_changes": self.metadata_changes,
            "ownership_manifest": self.ownership_manifest,
            "transformations": list(self.transformations),
            "writer_versions": dict(sorted(self.writer_versions.items())),
            "expected_inventory": list(self.expected_inventory),
            "verification_requirements": list(self.verification_requirements),
            "lifecycle_actions": list(self.lifecycle_actions),
            "planning_policy": self.planning_policy,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "blocked": self.blocked,
        }


@dataclass(frozen=True, slots=True)
class PlanDocument:
    plan_id: str
    created_at: str
    items: tuple[ResolvedItemSnapshot, ...]
    planning_policy: dict[str, Any]
    conflicts: tuple[PlanConflict, ...] = ()
    schema_version: int = PLAN_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "items": [item.to_dict() for item in self.items],
            "planning_policy": self.planning_policy,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def build_snapshot(
    *,
    item_id: str,
    source: SourcePrecondition,
    identity: CanonicalIdentity,
    projection: KavitaProjection | None,
    decision_provenance: dict[str, Any],
    transformations: tuple[dict[str, Any], ...],
    writer_versions: dict[str, str],
    expected_inventory: tuple[dict[str, Any], ...],
    verification_requirements: tuple[str, ...],
    retain_source: bool = False,
    lifecycle_policy: str = "move_after_verify",
    archive_path: str | None = None,
    destination_root: str | None = None,
    planning_policy: PlanningPolicySnapshot | None = None,
) -> ResolvedItemSnapshot:
    conflicts = tuple(
        PlanConflict("unresolved_identity", explanation)
        for explanation in identity.planning_blocks()
    )
    if projection is None and not conflicts:
        conflicts = (PlanConflict("missing_projection", "Kavita output projection is unresolved"),)
    if retain_source:
        lifecycle_policy = "preserve"
    if lifecycle_policy not in {"move_after_verify", "preserve", "archive_after_verify"}:
        raise ValueError(f"unsupported source lifecycle policy: {lifecycle_policy}")
    if lifecycle_policy == "archive_after_verify" and not archive_path:
        raise ValueError("archive_after_verify requires a planned archive path")
    if lifecycle_policy != "archive_after_verify" and archive_path is not None:
        raise ValueError("archive path is valid only for archive_after_verify")
    lifecycle_action: dict[str, Any] = {"action": lifecycle_policy}
    if archive_path:
        lifecycle_action["archive_path"] = archive_path
    lifecycle = (
        {"action": "stage_output"},
        {"action": "verify_staged_output"},
        {"action": "commit_destination"},
        lifecycle_action,
    )
    projection_dict = projection.to_dict() if projection else None
    if projection_dict is not None and destination_root is not None:
        assert projection is not None
        root = Path(destination_root).expanduser()
        if not root.is_absolute():
            raise ValueError("destination root embedded in a plan must be absolute")
        projection_dict["library_root"] = str(root)
        projection_dict["absolute_destination"] = str(root / projection.destination)
    ownership = (
        projection.ownership.to_dict()
        if projection
        else {
            "set": {},
            "clear": [],
            "preserve": [],
            "unresolved": list(identity.unresolved_fields),
        }
    )
    policy = planning_policy or default_planning_policy()
    return ResolvedItemSnapshot(
        item_id=item_id,
        source=source,
        canonical=identity.to_dict(),
        partial_resolution={
            "level": identity.resolution.value,
            "unresolved_fields": list(identity.unresolved_fields),
        },
        provenance=decision_provenance,
        kavita_projection=projection_dict,
        metadata_changes=ownership,
        ownership_manifest=ownership,
        transformations=transformations,
        writer_versions=writer_versions,
        expected_inventory=expected_inventory,
        verification_requirements=verification_requirements,
        lifecycle_actions=lifecycle,
        planning_policy=policy.to_dict(),
        conflicts=conflicts,
    )


def new_plan(
    plan_id: str,
    items: tuple[ResolvedItemSnapshot, ...],
    planning_policy: PlanningPolicySnapshot | None = None,
) -> PlanDocument:
    policy = planning_policy or default_planning_policy()
    if any(item.planning_policy != policy.to_dict() for item in items):
        raise ValueError("every plan item must carry the plan's exact planning policy")
    destinations: dict[str, list[str]] = {}
    for item in items:
        projection = item.kavita_projection
        if projection:
            destination = _immutable_destination_identity(projection)
            destinations.setdefault(destination, []).append(item.item_id)
    conflicts = tuple(
        PlanConflict(
            "destination_collision", "multiple items project to the same destination", tuple(ids)
        )
        for ids in destinations.values()
        if len(ids) > 1
    )
    return PlanDocument(
        plan_id,
        datetime.now(UTC).isoformat(),
        items,
        policy.to_dict(),
        conflicts,
    )


def _immutable_destination_identity(projection: dict[str, Any]) -> str:
    absolute = projection.get("absolute_destination")
    if isinstance(absolute, str) and absolute:
        return f"absolute:{PurePosixPath(absolute).as_posix().casefold()}"
    root = projection.get("library_root")
    destination = str(projection["destination"])
    if isinstance(root, str) and root:
        path = PurePosixPath(root) / PurePosixPath(destination)
        return f"absolute:{path.as_posix().casefold()}"
    return f"legacy-relative:{PurePosixPath(destination).as_posix().casefold()}"


def validate_plan_payload(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid plan JSON: {exc}") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") not in SUPPORTED_PLAN_SCHEMA_VERSIONS
    ):
        raise ValueError(
            "unsupported plan schema version; expected one of "
            f"{sorted(SUPPORTED_PLAN_SCHEMA_VERSIONS)}"
        )
    items = document.get("items")
    if not isinstance(items, list):
        raise ValueError("plan items must be a list")
    if document["schema_version"] == PLAN_SCHEMA_VERSION:
        policy = document.get("planning_policy")
        if not isinstance(policy, dict) or policy.get("version") not in {1, 2}:
            raise ValueError("schema 2 plans require supported planning_policy version 1 or 2")
        if policy.get("version") == 2:
            permissions = policy.get("permissions")
            if not isinstance(permissions, dict):
                raise ValueError("planning_policy version 2 requires permissions")
            _validate_permission_value(permissions.get("file_mode"), directory=False)
            _validate_permission_value(permissions.get("directory_mode"), directory=True)
    else:
        policy = None
    for item in items:
        if not isinstance(item, dict) or "item_id" not in item or "source" not in item:
            raise ValueError("each plan item requires item_id and source")
        projection = item.get("kavita_projection")
        provenance = item.get("provenance")
        if projection is not None and (
            not isinstance(provenance, dict) or provenance.get("explicit_approval") is not True
        ):
            raise ValueError("every projected item requires an explicit approval decision")
        if policy is not None and item.get("planning_policy") != policy:
            raise ValueError("plan item policy must equal the authoritative plan policy")
    canonical = json.dumps(
        document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    if canonical != payload:
        raise ValueError("plan is not in authoritative canonical JSON encoding")
    return document


def destination_from_item(item: dict[str, Any]) -> PurePosixPath | None:
    projection = item.get("kavita_projection")
    return PurePosixPath(projection["destination"]) if isinstance(projection, dict) else None


def _validate_permission_value(value: object, *, directory: bool) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"0[0-7]{3}", value):
        raise ValueError("planned permission modes must be four-digit octal strings")
    mode = int(value, 8)
    if mode & 0o002 or (directory and mode & 0o700 != 0o700) or (not directory and mode & 0o111):
        raise ValueError("planned permission mode violates publication safety policy")
