from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .apply_journal import ItemState, RunState
from .filesystem import sha256_file
from .scanner import ScanResult


@dataclass(frozen=True, slots=True)
class CompletedSource:
    scan: ScanResult
    destination: Path
    expected_destination_hash: str


@dataclass(frozen=True, slots=True)
class CompletedSourceWarning:
    scan: ScanResult
    destination: Path
    condition: str


@dataclass(frozen=True, slots=True)
class CompletedSourceAssessment:
    current: tuple[ScanResult, ...]
    completed: tuple[CompletedSource, ...]
    warnings: tuple[CompletedSourceWarning, ...]


def assess_completed_sources(
    connection: sqlite3.Connection, scans: list[ScanResult]
) -> CompletedSourceAssessment:
    current: list[ScanResult] = []
    completed: list[CompletedSource] = []
    warnings: list[CompletedSourceWarning] = []
    for scanned in scans:
        row = connection.execute(
            "SELECT i.destination_path, i.destination_hash FROM apply_items i "
            "JOIN apply_runs r ON r.id=i.run_id "
            "WHERE i.state=? AND r.status=? AND i.source_path=? "
            "AND i.planned_source_hash=? ORDER BY i.completed_at DESC LIMIT 1",
            (
                ItemState.COMPLETE.value,
                RunState.COMPLETE.value,
                str(scanned.source.path),
                scanned.source.sha256,
            ),
        ).fetchone()
        if row is None:
            current.append(scanned)
            continue
        destination = Path(str(row["destination_path"]))
        expected_hash = str(row["destination_hash"] or "")
        if not destination.is_file():
            current.append(scanned)
            warnings.append(CompletedSourceWarning(scanned, destination, "destination_missing"))
            continue
        if not expected_hash or sha256_file(destination) != expected_hash:
            current.append(scanned)
            warnings.append(CompletedSourceWarning(scanned, destination, "destination_mismatch"))
            continue
        completed.append(CompletedSource(scanned, destination, expected_hash))
    return CompletedSourceAssessment(tuple(current), tuple(completed), tuple(warnings))
