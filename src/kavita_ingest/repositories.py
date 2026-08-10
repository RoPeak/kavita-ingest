from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime

from .domain import Classification, InspectionResult, SourceRecord


class SourceRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def upsert(self, source: SourceRecord) -> int:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            INSERT INTO sources(path, size, mtime_ns, sha256, format, signature,
                                first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size=excluded.size, mtime_ns=excluded.mtime_ns, sha256=excluded.sha256,
                format=excluded.format, signature=excluded.signature,
                last_seen_at=excluded.last_seen_at
            """,
            (
                str(source.path),
                source.size,
                source.mtime_ns,
                source.sha256,
                source.format.value,
                source.signature,
                now,
                now,
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM sources WHERE path = ?", (str(source.path),)
        ).fetchone()
        if row is None:
            raise RuntimeError("source upsert did not return a row")
        return int(row[0])

    def add_inspection(self, source_id: int, result: InspectionResult) -> None:
        self.connection.execute(
            """
            INSERT INTO inspections(source_id, inspected_at, status, metadata_json,
              evidence_json, warnings_json, error_code, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                datetime.now(UTC).isoformat(),
                result.status.value,
                json.dumps(result.metadata, sort_keys=True, default=str),
                json.dumps([asdict(item) for item in result.evidence], sort_keys=True),
                json.dumps(result.warnings),
                result.error_code,
                result.error_message,
            ),
        )

    def add_classification(self, source_id: int, result: Classification) -> None:
        self.connection.execute(
            """
            INSERT INTO classifications(source_id, classified_at, kind, subtype,
              confidence, ambiguous, hypotheses_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                datetime.now(UTC).isoformat(),
                result.kind.value,
                result.subtype,
                result.confidence,
                int(result.ambiguous),
                json.dumps(
                    [asdict(item) for item in result.hypotheses], sort_keys=True, default=str
                ),
            ),
        )
