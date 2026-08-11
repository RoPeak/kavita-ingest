from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from .canonical import CanonicalIdentity
from .projection import KavitaProjection

PLAN_SCHEMA_VERSION = 1


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
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "blocked": self.blocked,
        }


@dataclass(frozen=True, slots=True)
class PlanDocument:
    plan_id: str
    created_at: str
    items: tuple[ResolvedItemSnapshot, ...]
    conflicts: tuple[PlanConflict, ...] = ()
    schema_version: int = PLAN_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "items": [item.to_dict() for item in self.items],
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
) -> ResolvedItemSnapshot:
    conflicts = tuple(
        PlanConflict("unresolved_identity", explanation)
        for explanation in identity.planning_blocks()
    )
    if projection is None and not conflicts:
        conflicts = (PlanConflict("missing_projection", "Kavita output projection is unresolved"),)
    lifecycle = (
        {"action": "stage_output"},
        {"action": "verify_staged_output"},
        {"action": "commit_destination"},
        {"action": "retain_source" if retain_source else "remove_source_after_verified_commit"},
    )
    projection_dict = projection.to_dict() if projection else None
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
        conflicts=conflicts,
    )


def new_plan(plan_id: str, items: tuple[ResolvedItemSnapshot, ...]) -> PlanDocument:
    destinations: dict[str, list[str]] = {}
    for item in items:
        projection = item.kavita_projection
        if projection:
            destination = str(projection["destination"])
            destinations.setdefault(destination.casefold(), []).append(item.item_id)
    conflicts = tuple(
        PlanConflict(
            "destination_collision", "multiple items project to the same destination", tuple(ids)
        )
        for ids in destinations.values()
        if len(ids) > 1
    )
    return PlanDocument(plan_id, datetime.now(UTC).isoformat(), items, conflicts)


def validate_plan_payload(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid plan JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError(f"unsupported plan schema version; expected {PLAN_SCHEMA_VERSION}")
    items = document.get("items")
    if not isinstance(items, list):
        raise ValueError("plan items must be a list")
    for item in items:
        if not isinstance(item, dict) or "item_id" not in item or "source" not in item:
            raise ValueError("each plan item requires item_id and source")
        projection = item.get("kavita_projection")
        provenance = item.get("provenance")
        if projection is not None and (
            not isinstance(provenance, dict) or provenance.get("explicit_approval") is not True
        ):
            raise ValueError("every projected item requires an explicit approval decision")
    canonical = json.dumps(
        document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    if canonical != payload:
        raise ValueError("plan is not in authoritative canonical JSON encoding")
    return document


def destination_from_item(item: dict[str, Any]) -> PurePosixPath | None:
    projection = item.get("kavita_projection")
    return PurePosixPath(projection["destination"]) if isinstance(projection, dict) else None
