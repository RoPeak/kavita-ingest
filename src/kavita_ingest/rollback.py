from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .apply_journal import ItemState, JournalRepository
from .db import connect, migrate
from .filesystem import sha256_file
from .locking import ProcessLock, lock_path
from .plan_store import PlanStore


@dataclass(frozen=True, slots=True)
class RollbackPreview:
    item_id: str
    reversible: bool
    action: str
    explanation: str


def preview_rollback(database_path: Path, plan_id: int) -> tuple[RollbackPreview, ...]:
    with ProcessLock(lock_path(database_path)):
        migrate(database_path)
        with connect(database_path) as connection:
            plan = PlanStore(connection).get(plan_id)
            journal = JournalRepository(connection)
            run = journal.latest_for_plan(plan_id)
            if run is None:
                raise ValueError(f"plan {plan_id} has no apply run")
            if run.plan_digest != plan.sha256:
                raise ValueError("apply run digest differs from immutable plan")
            output: list[RollbackPreview] = []
            for item in journal.items(run.id):
                output.append(_preview_item(item))
            return tuple(output)


def _preview_item(item: object) -> RollbackPreview:
    from .apply_journal import ApplyItem

    if not isinstance(item, ApplyItem):
        raise TypeError("rollback preview requires an apply journal item")
    if item.state not in {
        ItemState.COMMITTED,
        ItemState.CLEANUP_PENDING,
        ItemState.CLEANED,
        ItemState.COMPLETE,
    }:
        return RollbackPreview(
            item.item_id, False, "none", f"item has not reached committed state ({item.state})"
        )
    destination = Path(item.destination_path)
    expected = item.destination_hash or item.staged_hash
    if not expected or not destination.is_file():
        return RollbackPreview(item.item_id, False, "manual", "verified destination is missing")
    if sha256_file(destination) != expected:
        return RollbackPreview(
            item.item_id,
            False,
            "refuse",
            "destination changed after ingestion and must not be removed automatically",
        )
    source = Path(item.source_path)
    source_valid = source.is_file() and sha256_file(source) == item.planned_source_hash
    if item.lifecycle_policy == "preserve" and source_valid:
        return RollbackPreview(
            item.item_id,
            True,
            "remove_unchanged_destination",
            "the exact original source is still preserved",
        )
    if item.lifecycle_policy == "archive_after_verify" and item.archive_path:
        archive = Path(item.archive_path)
        archive_valid = archive.is_file() and sha256_file(archive) == item.planned_source_hash
        if source_valid:
            return RollbackPreview(
                item.item_id,
                True,
                "remove_unchanged_destination",
                "the exact source and archive remain available",
            )
        if archive_valid and not source.exists():
            return RollbackPreview(
                item.item_id,
                True,
                "restore_archive_then_remove_destination",
                "the exact archived original can be restored without overwriting",
            )
    if item.lifecycle_policy == "move_after_verify" and not source.exists():
        return RollbackPreview(
            item.item_id,
            False,
            "impossible",
            "the original was deleted after verification and no retained copy can reconstruct it",
        )
    return RollbackPreview(
        item.item_id,
        False,
        "manual",
        "the available files do not prove a safe reversible rollback",
    )
