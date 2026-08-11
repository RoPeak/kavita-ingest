from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

LOGGER = logging.getLogger(__name__)


class RunState(StrEnum):
    PREFLIGHTING = "preflighting"
    RUNNING = "running"
    RECOVERY_REQUIRED = "recovery_required"
    FAILED = "failed"
    COMPLETE = "complete"


class ItemState(StrEnum):
    PENDING = "pending"
    PREFLIGHT_OK = "preflight_ok"
    STAGING = "staging"
    STAGED = "staged"
    VERIFIED = "verified"
    COMMITTING = "committing"
    COMMITTED = "committed"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANED = "cleaned"
    COMPLETE = "complete"
    FAILED = "failed"
    STALE = "stale"
    RECOVERY_REQUIRED = "recovery_required"


TERMINAL_ITEM_STATES = {ItemState.COMPLETE, ItemState.STALE, ItemState.RECOVERY_REQUIRED}

ALLOWED_TRANSITIONS: dict[ItemState, set[ItemState]] = {
    ItemState.PENDING: {
        ItemState.PREFLIGHT_OK,
        ItemState.STALE,
        ItemState.FAILED,
        ItemState.RECOVERY_REQUIRED,
    },
    ItemState.PREFLIGHT_OK: {ItemState.STAGING, ItemState.STALE, ItemState.RECOVERY_REQUIRED},
    ItemState.STAGING: {
        ItemState.STAGED,
        ItemState.FAILED,
        ItemState.STALE,
        ItemState.RECOVERY_REQUIRED,
    },
    ItemState.STAGED: {
        ItemState.VERIFIED,
        ItemState.FAILED,
        ItemState.STALE,
        ItemState.RECOVERY_REQUIRED,
    },
    ItemState.VERIFIED: {ItemState.COMMITTING, ItemState.STALE, ItemState.RECOVERY_REQUIRED},
    ItemState.COMMITTING: {
        ItemState.COMMITTED,
        ItemState.STALE,
        ItemState.RECOVERY_REQUIRED,
    },
    ItemState.COMMITTED: {
        ItemState.CLEANUP_PENDING,
        ItemState.COMPLETE,
        ItemState.RECOVERY_REQUIRED,
    },
    ItemState.CLEANUP_PENDING: {ItemState.CLEANED, ItemState.RECOVERY_REQUIRED},
    ItemState.CLEANED: {ItemState.COMPLETE, ItemState.RECOVERY_REQUIRED},
    ItemState.FAILED: {ItemState.STAGING, ItemState.STALE, ItemState.RECOVERY_REQUIRED},
    ItemState.COMPLETE: set(),
    ItemState.STALE: set(),
    ItemState.RECOVERY_REQUIRED: set(),
}


@dataclass(frozen=True, slots=True)
class ApplyRun:
    id: str
    plan_id: int
    plan_digest: str
    status: RunState
    started_at: str
    updated_at: str
    completed_at: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ApplyItem:
    run_id: str
    item_id: str
    state: ItemState
    source_path: str
    planned_source_hash: str
    staging_path: str | None
    staged_hash: str | None
    staged_size: int | None
    verification: dict[str, Any]
    destination_path: str
    destination_hash: str | None
    lifecycle_policy: str
    archive_path: str | None
    cleanup_path: str | None
    error: str | None
    recovery_detail: str | None


class JournalRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA busy_timeout = 5000")

    def create_run(
        self, plan_id: int, plan_digest: str, items: list[dict[str, str | None]]
    ) -> ApplyRun:
        run_id = str(uuid.uuid4())
        now = _now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT INTO apply_runs(id, plan_id, plan_digest, status, started_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, plan_id, plan_digest, RunState.PREFLIGHTING.value, now, now),
            )
            for item in items:
                self.connection.execute(
                    "INSERT INTO apply_items(run_id, item_id, state, source_path, "
                    "planned_source_hash, destination_path, lifecycle_policy, archive_path, "
                    "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        item["item_id"],
                        ItemState.PENDING.value,
                        item["source_path"],
                        item["planned_source_hash"],
                        item["destination_path"],
                        item["lifecycle_policy"],
                        item["archive_path"],
                        now,
                        now,
                    ),
                )
                self._event(run_id, str(item["item_id"]), None, ItemState.PENDING.value, {})
            self.connection.commit()
        except sqlite3.Error:
            self.connection.rollback()
            raise
        return self.get_run(run_id)

    def transition(
        self,
        run_id: str,
        item_id: str,
        to_state: ItemState,
        *,
        detail: dict[str, Any] | None = None,
        fields: dict[str, Any] | None = None,
    ) -> ApplyItem:
        current = self.get_item(run_id, item_id)
        if to_state not in ALLOWED_TRANSITIONS[current.state]:
            raise ValueError(
                f"invalid journal transition: {current.state.value} -> {to_state.value}"
            )
        now = _now()
        updates = {"state": to_state.value, "updated_at": now, **(fields or {})}
        if to_state is ItemState.COMPLETE:
            updates["completed_at"] = now
        assignments = ", ".join(f"{key}=?" for key in updates)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            cursor = self.connection.execute(
                f"UPDATE apply_items SET {assignments} WHERE run_id=? AND item_id=? AND state=?",
                (*updates.values(), run_id, item_id, current.state.value),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("journal item state changed concurrently")
            self._event(run_id, item_id, current.state.value, to_state.value, detail or {})
            self.connection.commit()
        except (sqlite3.Error, RuntimeError):
            self.connection.rollback()
            raise
        LOGGER.info(
            "apply run=%s item=%s transition=%s->%s",
            run_id,
            item_id,
            current.state.value,
            to_state.value,
        )
        return self.get_item(run_id, item_id)

    def set_run_state(self, run_id: str, state: RunState, *, error: str | None = None) -> ApplyRun:
        now = _now()
        completed = now if state is RunState.COMPLETE else None
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "UPDATE apply_runs SET status=?, updated_at=?, completed_at=?, error=? WHERE id=?",
                (state.value, now, completed, error, run_id),
            )
            self._event(run_id, None, None, f"run:{state.value}", {"error": error})
            self.connection.commit()
        except sqlite3.Error:
            self.connection.rollback()
            raise
        return self.get_run(run_id)

    def note_error(self, run_id: str, item_id: str, message: str) -> ApplyItem:
        current = self.get_item(run_id, item_id)
        now = _now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "UPDATE apply_items SET updated_at=?, error=?, recovery_detail=? "
                "WHERE run_id=? AND item_id=?",
                (now, message, message, run_id, item_id),
            )
            self._event(
                run_id,
                item_id,
                current.state.value,
                current.state.value,
                {"error": message, "recovery_required": True},
            )
            self.connection.commit()
        except sqlite3.Error:
            self.connection.rollback()
            raise
        return self.get_item(run_id, item_id)

    def get_run(self, run_id: str) -> ApplyRun:
        row = self.connection.execute("SELECT * FROM apply_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"apply run {run_id} does not exist")
        return _run(row)

    def latest_for_plan(self, plan_id: int) -> ApplyRun | None:
        row = self.connection.execute(
            "SELECT * FROM apply_runs WHERE plan_id=? ORDER BY started_at DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        return _run(row) if row else None

    def items(self, run_id: str) -> tuple[ApplyItem, ...]:
        rows = self.connection.execute(
            "SELECT * FROM apply_items WHERE run_id=? ORDER BY rowid", (run_id,)
        ).fetchall()
        return tuple(_item(row) for row in rows)

    def get_item(self, run_id: str, item_id: str) -> ApplyItem:
        row = self.connection.execute(
            "SELECT * FROM apply_items WHERE run_id=? AND item_id=?", (run_id, item_id)
        ).fetchone()
        if row is None:
            raise KeyError(f"apply item {run_id}/{item_id} does not exist")
        return _item(row)

    def _event(
        self,
        run_id: str,
        item_id: str | None,
        from_state: str | None,
        to_state: str,
        detail: dict[str, Any],
    ) -> None:
        self.connection.execute(
            "INSERT INTO apply_journal_events(run_id, item_id, from_state, to_state, "
            "occurred_at, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                item_id,
                from_state,
                to_state,
                _now(),
                json.dumps(detail, sort_keys=True, separators=(",", ":")),
            ),
        )


def _run(row: sqlite3.Row) -> ApplyRun:
    return ApplyRun(
        row["id"],
        int(row["plan_id"]),
        row["plan_digest"],
        RunState(row["status"]),
        row["started_at"],
        row["updated_at"],
        row["completed_at"],
        row["error"],
    )


def _item(row: sqlite3.Row) -> ApplyItem:
    return ApplyItem(
        row["run_id"],
        row["item_id"],
        ItemState(row["state"]),
        row["source_path"],
        row["planned_source_hash"],
        row["staging_path"],
        row["staged_hash"],
        row["staged_size"],
        json.loads(row["verification_json"] or "{}"),
        row["destination_path"],
        row["destination_hash"],
        row["lifecycle_policy"],
        row["archive_path"],
        row["cleanup_path"],
        row["error"],
        row["recovery_detail"],
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
