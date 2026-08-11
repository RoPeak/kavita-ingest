from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .matching import CandidateScore


class RunGroupDecisionType(StrEnum):
    SELECTED = "selected"
    MANUAL = "manual"
    CLEARED = "cleared"


@dataclass(frozen=True, slots=True)
class RunGroupDecision:
    id: int
    group_key: str
    provider: str
    decision_type: RunGroupDecisionType
    provider_run_id: str | None
    run_snapshot: dict[str, Any]
    created_at: str
    supersedes_id: int | None

    @property
    def active(self) -> bool:
        return self.decision_type is not RunGroupDecisionType.CLEARED


class RunGroupRepository:
    """Append-only, user-directed provider run selections for a local series group."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def choose(
        self,
        group_key: str,
        provider: str,
        provider_run_id: str,
        snapshot: dict[str, Any],
        *,
        manual: bool = False,
    ) -> RunGroupDecision:
        if not provider_run_id.strip():
            raise ValueError("provider run id cannot be empty")
        return self._add(
            group_key,
            provider,
            RunGroupDecisionType.MANUAL if manual else RunGroupDecisionType.SELECTED,
            provider_run_id,
            snapshot,
        )

    def clear(self, group_key: str, provider: str) -> RunGroupDecision:
        if self.latest(group_key, provider) is None:
            raise ValueError("run group has no decision to clear")
        return self._add(group_key, provider, RunGroupDecisionType.CLEARED, None, {})

    def latest(self, group_key: str, provider: str) -> RunGroupDecision | None:
        row = self.connection.execute(
            "SELECT * FROM run_group_decisions WHERE group_key=? AND provider=? "
            "ORDER BY id DESC LIMIT 1",
            (group_key, provider),
        ).fetchone()
        return _record(row) if row else None

    def history(self, group_key: str, provider: str) -> tuple[RunGroupDecision, ...]:
        rows = self.connection.execute(
            "SELECT * FROM run_group_decisions WHERE group_key=? AND provider=? ORDER BY id",
            (group_key, provider),
        ).fetchall()
        return tuple(_record(row) for row in rows)

    def _add(
        self,
        group_key: str,
        provider: str,
        decision_type: RunGroupDecisionType,
        provider_run_id: str | None,
        snapshot: dict[str, Any],
    ) -> RunGroupDecision:
        previous = self.latest(group_key, provider)
        created_at = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            "INSERT INTO run_group_decisions(group_key, provider, decision_type, "
            "provider_run_id, run_snapshot_json, created_at, supersedes_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                group_key,
                provider,
                decision_type.value,
                provider_run_id,
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
                created_at,
                previous.id if previous else None,
            ),
        )
        self.connection.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("run-group decision insert returned no id")
        return RunGroupDecision(
            int(cursor.lastrowid),
            group_key,
            provider,
            decision_type,
            provider_run_id,
            snapshot,
            created_at,
            previous.id if previous else None,
        )


def constrain_to_selected_run(
    scores: tuple[CandidateScore, ...], decision: RunGroupDecision | None
) -> tuple[CandidateScore, ...]:
    """Constrain issue candidates without accepting any item-level identity."""
    if decision is None or not decision.active or decision.provider_run_id is None:
        return scores
    return tuple(score for score in scores if score.candidate.run_id == decision.provider_run_id)


def run_group_key(series_title: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", series_title.casefold()).strip()
    if not normalized:
        raise ValueError("series title cannot produce an empty run-group key")
    return f"comic:{normalized}"


def _record(row: sqlite3.Row) -> RunGroupDecision:
    return RunGroupDecision(
        int(row["id"]),
        row["group_key"],
        row["provider"],
        RunGroupDecisionType(row["decision_type"]),
        row["provider_run_id"],
        json.loads(row["run_snapshot_json"]),
        row["created_at"],
        row["supersedes_id"],
    )
