from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .planning import (
    LEGACY_POLICY_MESSAGE,
    PlanDocument,
    destination_from_item,
    planning_policy_version,
    require_current_planning_policy,
    validate_plan_payload,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StoredPlan:
    id: int
    schema_version: int
    canonical_json: bytes
    byte_length: int
    sha256: str
    status: str
    created_at: str
    approved_at: str | None
    approval_digest: str | None


class PlanStore:
    """SQLite canonical bytes are authoritative; indexes and exports are derived."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, plan: PlanDocument) -> StoredPlan:
        return self.import_bytes(plan.canonical_bytes())

    def import_bytes(self, payload: bytes) -> StoredPlan:
        document = validate_plan_payload(payload)
        digest = hashlib.sha256(payload).hexdigest()
        created_at = str(document.get("created_at") or datetime.now(UTC).isoformat())
        try:
            cursor = self.connection.execute(
                "INSERT INTO plans(schema_version, canonical_json, byte_length, sha256, status, "
                "created_at) VALUES (?, ?, ?, ?, 'draft', ?)",
                (int(document["schema_version"]), payload, len(payload), digest, created_at),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("plan insert returned no id")
            plan_id = int(cursor.lastrowid)
            self._derive_indexes(plan_id, document)
            self._supersede_prior_unapplied_plans(plan_id)
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise ValueError(
                f"plan already exists or failed integrity constraints: {digest}"
            ) from exc
        LOGGER.info("created immutable draft plan=%s digest=%s", plan_id, digest[:12])
        return self.get(plan_id)

    def get(self, plan_id: int) -> StoredPlan:
        row = self.connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(f"plan {plan_id} does not exist")
        plan = _record(row)
        self._verify(plan)
        return plan

    def approve(self, plan_id: int, digest: str) -> StoredPlan:
        plan = self.get(plan_id)
        if digest != plan.sha256:
            raise ValueError("approval digest does not match the exact authoritative plan bytes")
        invalidated = self.connection.execute(
            "SELECT reason FROM plan_invalidations WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if invalidated:
            raise ValueError(f"invalidated plan cannot be approved: {invalidated[0]}")
        superseded = self.connection.execute(
            "SELECT new_plan_id FROM plan_supersessions WHERE old_plan_id=?", (plan_id,)
        ).fetchone()
        if superseded:
            raise ValueError(f"superseded plan cannot be approved; use plan {superseded[0]}")
        document = validate_plan_payload(plan.canonical_json)
        try:
            require_current_planning_policy(document)
        except ValueError as exc:
            self._mark_incompatible(plan_id, LEGACY_POLICY_MESSAGE)
            raise ValueError(LEGACY_POLICY_MESSAGE) from exc
        if document.get("conflicts") or any(item.get("blocked") for item in document["items"]):
            raise ValueError("plans with unresolved conflicts cannot be approved")
        approved_at = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            "UPDATE plans SET status='approved', approved_at=?, approval_digest=? "
            "WHERE id=? AND status='draft'",
            (approved_at, digest, plan_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("only a draft plan can be approved")
        self.connection.commit()
        LOGGER.info("approved immutable plan=%s digest=%s", plan_id, digest[:12])
        return self.get(plan_id)

    def policy_version(self, plan_id: int) -> int | None:
        plan = self.get(plan_id)
        return planning_policy_version(validate_plan_payload(plan.canonical_json))

    def _mark_incompatible(self, plan_id: int, reason: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO plan_invalidations(plan_id, reason, invalidated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, reason, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def export(self, plan_id: int, destination: Path) -> None:
        plan = self.get(plan_id)
        destination.write_bytes(plan.canonical_json)

    def _derive_indexes(self, plan_id: int, document: dict[str, Any]) -> None:
        for item in document["items"]:
            destination = destination_from_item(item)
            self.connection.execute(
                "INSERT INTO plan_items_index(plan_id, item_id, source_fingerprint, "
                "destination_path, blocked) VALUES (?, ?, ?, ?, ?)",
                (
                    plan_id,
                    str(item["item_id"]),
                    str(item["source"]["sha256"]),
                    destination.as_posix() if destination else None,
                    int(bool(item.get("blocked", False))),
                ),
            )
            provenance = item.get("provenance", {})
            decision_head = (
                provenance.get("decision_head_id") if isinstance(provenance, dict) else None
            )
            if decision_head is not None:
                self.connection.execute(
                    "INSERT INTO plan_preconditions(plan_id, item_id, source_fingerprint, "
                    "decision_head_id, run_group_key, run_group_decision_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        plan_id,
                        str(item["item_id"]),
                        str(item["source"]["sha256"]),
                        int(decision_head),
                        provenance.get("run_group_key"),
                        provenance.get("run_group_decision_id"),
                    ),
                )

    def _supersede_prior_unapplied_plans(self, plan_id: int) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO plan_supersessions(old_plan_id, new_plan_id, superseded_at) "
            "SELECT DISTINCT old.plan_id, ?, ? FROM plan_items_index old "
            "JOIN plan_items_index new ON new.plan_id=? "
            "AND new.source_fingerprint=old.source_fingerprint "
            "WHERE old.plan_id<>? AND NOT EXISTS "
            "(SELECT 1 FROM apply_runs WHERE apply_runs.plan_id=old.plan_id)",
            (plan_id, datetime.now(UTC).isoformat(), plan_id, plan_id),
        )

    @staticmethod
    def _verify(plan: StoredPlan) -> None:
        if len(plan.canonical_json) != plan.byte_length:
            raise RuntimeError("stored plan byte length is corrupt")
        if hashlib.sha256(plan.canonical_json).hexdigest() != plan.sha256:
            raise RuntimeError("stored plan digest is corrupt")
        validate_plan_payload(plan.canonical_json)


def _record(row: sqlite3.Row) -> StoredPlan:
    payload = row["canonical_json"]
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return StoredPlan(
        int(row["id"]),
        int(row["schema_version"]),
        bytes(payload),
        int(row["byte_length"]),
        row["sha256"],
        row["status"],
        row["created_at"],
        row["approved_at"],
        row["approval_digest"],
    )
