from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from .domain import SequenceNumber, SourceRecord
from .matching import CandidateScore, Reconciliation
from .providers.models import RecordType

LOGGER = logging.getLogger(__name__)


class DecisionType(StrEnum):
    ACCEPTED = "accepted"
    WORK_ACCEPTED = "work_accepted"
    REJECTED = "rejected"
    MANUAL_OVERRIDE = "manual_override"
    MANUAL_IDENTITY = "manual_identity"
    UNRESOLVED = "unresolved"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    id: int
    source_fingerprint: str
    media_signature: str
    decision_type: DecisionType
    candidate_key: str | None
    source_evidence_hash: str
    candidate_data_hash: str | None
    payload: dict[str, Any]
    created_at: str
    supersedes_id: int | None
    batch_id: str | None


class DecisionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(
        self,
        source: SourceRecord,
        decision_type: DecisionType,
        source_evidence_hash: str,
        *,
        candidate_key: str | None = None,
        candidate_data_hash: str | None = None,
        payload: dict[str, Any] | None = None,
        batch_id: str | None = None,
    ) -> DecisionRecord:
        previous = self.latest(source)
        created_at = datetime.now(UTC).isoformat()
        media_signature = _media_signature(source)
        cursor = self.connection.execute(
            """
            INSERT INTO decisions(source_fingerprint, media_signature, decision_type,
              candidate_key, source_evidence_hash, candidate_data_hash, payload_json,
              created_at, supersedes_id, batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.sha256,
                media_signature,
                decision_type.value,
                candidate_key,
                source_evidence_hash,
                candidate_data_hash,
                json.dumps(payload or {}, sort_keys=True, default=str),
                created_at,
                previous.id if previous else None,
                batch_id,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("decision insert did not return an identifier")
        decision_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT OR IGNORE INTO plan_invalidations(plan_id, reason, invalidated_at) "
            "SELECT DISTINCT plan_id, ?, ? FROM plan_preconditions "
            "WHERE source_fingerprint=? AND decision_head_id<>?",
            (
                "explicit identity decision changed after plan creation",
                created_at,
                source.sha256,
                decision_id,
            ),
        )
        self.connection.commit()
        LOGGER.info(
            "recorded explicit decision id=%s type=%s source=%s",
            decision_id,
            decision_type.value,
            source.sha256[:12],
        )
        return DecisionRecord(
            decision_id,
            source.sha256,
            media_signature,
            decision_type,
            candidate_key,
            source_evidence_hash,
            candidate_data_hash,
            payload or {},
            created_at,
            previous.id if previous else None,
            batch_id,
        )

    def latest(self, source: SourceRecord) -> DecisionRecord | None:
        row = self.connection.execute(
            "SELECT * FROM decisions WHERE source_fingerprint=? AND media_signature=? "
            "ORDER BY id DESC LIMIT 1",
            (source.sha256, _media_signature(source)),
        ).fetchone()
        return _record(row) if row else None

    def history(self, source: SourceRecord) -> tuple[DecisionRecord, ...]:
        rows = self.connection.execute(
            "SELECT * FROM decisions WHERE source_fingerprint=? AND media_signature=? ORDER BY id",
            (source.sha256, _media_signature(source)),
        ).fetchall()
        return tuple(_record(row) for row in rows)

    def rejection_suppresses(
        self,
        source: SourceRecord,
        candidate_key: str,
        source_evidence_hash: str,
        candidate_data_hash: str,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT decision_type, source_evidence_hash, candidate_data_hash
            FROM decisions
            WHERE source_fingerprint=? AND media_signature=? AND candidate_key=?
            ORDER BY id DESC LIMIT 1
            """,
            (source.sha256, _media_signature(source), candidate_key),
        ).fetchone()
        return bool(
            row
            and row[0] == DecisionType.REJECTED.value
            and row[1] == source_evidence_hash
            and row[2] == candidate_data_hash
        )

    def manual_overrides(self, source: SourceRecord) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for decision in self.history(source):
            if decision.decision_type is DecisionType.MANUAL_OVERRIDE:
                overrides = decision.payload.get("overrides", {})
                if isinstance(overrides, dict):
                    output.update(overrides)
                cleared = decision.payload.get("cleared_fields", [])
                if isinstance(cleared, list):
                    for field in cleared:
                        output.pop(str(field), None)
        return output

    def clear_rejection(
        self,
        source: SourceRecord,
        candidate_key: str,
        source_evidence_hash: str,
    ) -> DecisionRecord:
        return self.add(
            source,
            DecisionType.UNRESOLVED,
            source_evidence_hash,
            candidate_key=candidate_key,
            payload={"cleared_rejection": True},
        )


def accept_candidate(
    repository: DecisionRepository,
    source: SourceRecord,
    score: CandidateScore,
    reconciliation: Reconciliation,
    source_evidence_hash: str,
    *,
    work_only: bool = False,
    batch_id: str | None = None,
    hydration: dict[str, Any] | None = None,
) -> DecisionRecord:
    if score.candidate.record_type is RecordType.COMIC_RUN:
        raise ValueError(
            "comic run records are run-group context, not media identities; "
            "select the run group and then accept an issue candidate"
        )
    work_only = work_only or score.candidate.record_type is RecordType.BOOK_WORK
    decision_type = DecisionType.WORK_ACCEPTED if work_only else DecisionType.ACCEPTED
    return repository.add(
        source,
        decision_type,
        source_evidence_hash,
        candidate_key=score.candidate.key,
        candidate_data_hash=score.candidate.data_hash(),
        payload={
            "candidate": score.candidate.to_dict(),
            "score": score.score,
            "reconciliation": asdict(reconciliation),
            "hydration": hydration or {"status": "not_requested"},
            "explicit": True,
        },
        batch_id=batch_id,
    )


def add_manual_override(
    repository: DecisionRepository,
    source: SourceRecord,
    source_evidence_hash: str,
    field: str,
    value: str,
) -> DecisionRecord:
    resolved = validate_manual_override(field, value)
    overrides = repository.manual_overrides(source)
    overrides[field] = resolved
    return repository.add(
        source,
        DecisionType.MANUAL_OVERRIDE,
        source_evidence_hash,
        payload={"overrides": overrides, "provenance": "user"},
    )


def add_manual_identity(
    repository: DecisionRepository,
    source: SourceRecord,
    source_evidence_hash: str,
    fields: dict[str, str],
) -> DecisionRecord:
    resolved = {field: validate_manual_override(field, value) for field, value in fields.items()}
    if not resolved.get("title") and not resolved.get("series_title"):
        raise ValueError("manual identity requires a title or series title")
    return repository.add(
        source,
        DecisionType.MANUAL_IDENTITY,
        source_evidence_hash,
        payload={"identity": resolved, "provenance": "user", "explicit": True},
    )


def clear_manual_override(
    repository: DecisionRepository,
    source: SourceRecord,
    source_evidence_hash: str,
    field: str,
) -> DecisionRecord:
    if field not in repository.manual_overrides(source):
        raise ValueError(f"manual override {field!r} is not set")
    return repository.add(
        source,
        DecisionType.MANUAL_OVERRIDE,
        source_evidence_hash,
        payload={"cleared_fields": [field], "provenance": "user"},
    )


def batch_accept(
    repository: DecisionRepository,
    items: list[tuple[SourceRecord, CandidateScore, Reconciliation, str]],
    *,
    confirmed_count: int,
    hydration: dict[str, dict[str, Any]] | None = None,
) -> list[DecisionRecord]:
    eligible = batch_eligible_items(items)
    if confirmed_count != len(eligible):
        raise ValueError(f"batch confirmation must acknowledge exactly {len(eligible)} items")
    batch_id = str(uuid.uuid4())
    return [
        accept_candidate(
            repository,
            source,
            score,
            reconciliation,
            evidence_hash,
            batch_id=batch_id,
            hydration=(hydration or {}).get(score.candidate.key),
        )
        for source, score, reconciliation, evidence_hash in eligible
    ]


def batch_eligible_items(
    items: list[tuple[SourceRecord, CandidateScore, Reconciliation, str]],
) -> list[tuple[SourceRecord, CandidateScore, Reconciliation, str]]:
    """Return the single authoritative set eligible for explicit batch acceptance."""
    return [
        item
        for item in items
        if item[1].eligible
        and not item[1].suppressed
        and item[1].candidate.record_type is not RecordType.COMIC_RUN
        and item[2].edition_state != "unresolved"
    ]


def validate_manual_override(field: str, value: str) -> Any:
    cleaned = value.strip()
    if field in {"isbn", "isbn10", "isbn13"}:
        digits = re.sub(r"[^0-9Xx]", "", cleaned).upper()
        if not _valid_isbn(digits):
            raise ValueError("invalid ISBN checksum")
        return digits
    if field in {"sequence", "series_index"}:
        return SequenceNumber.parse(cleaned).normalized
    if field == "run_start_year":
        year = int(cleaned)
        if not 1800 <= year <= datetime.now(UTC).year + 1:
            raise ValueError("run_start_year is outside the supported range")
        return year
    if field == "collection_volume":
        volume = int(cleaned)
        if volume < 1:
            raise ValueError("collection_volume must be a positive integer")
        return volume
    if field in {"publication_date", "date"}:
        return date.fromisoformat(cleaned).isoformat()
    if field == "language":
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", cleaned):
            raise ValueError("language must be a valid language tag")
        return cleaned
    if field == "item_type":
        allowed = {
            "issue",
            "annual",
            "special",
            "one-shot",
            "trade",
            "collected-edition",
            "omnibus",
            "graphic-novel",
        }
        if cleaned.casefold() not in allowed:
            raise ValueError("unsupported comic item type")
        return cleaned.casefold()
    if field in {"creators", "authors", "translators", "editors", "illustrators"}:
        values = tuple(part.strip() for part in cleaned.split(",") if part.strip())
        if not values:
            raise ValueError("at least one contributor is required")
        return values
    if field in {"title", "series_title", "publisher"}:
        if not cleaned:
            raise ValueError(f"{field} cannot be empty")
        return cleaned
    raise ValueError(f"field {field!r} is not manually editable")


def _valid_isbn(value: str) -> bool:
    if len(value) == 10:
        digits = [
            10 if char == "X" and index == 9 else int(char) for index, char in enumerate(value)
        ]
        return sum((10 - index) * digit for index, digit in enumerate(digits)) % 11 == 0
    if len(value) == 13 and value.isdigit():
        total = sum(int(char) * (1 if index % 2 == 0 else 3) for index, char in enumerate(value))
        return total % 10 == 0
    return False


def _media_signature(source: SourceRecord) -> str:
    return f"{source.format.value}:{source.size}"


def _record(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        int(row["id"]),
        row["source_fingerprint"],
        row["media_signature"],
        DecisionType(row["decision_type"]),
        row["candidate_key"],
        row["source_evidence_hash"],
        row["candidate_data_hash"],
        json.loads(row["payload_json"]),
        row["created_at"],
        row["supersedes_id"],
        row["batch_id"],
    )
