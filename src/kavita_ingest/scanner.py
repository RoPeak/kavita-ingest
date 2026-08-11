from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .archive_safety import ArchiveLimits
from .config import AppConfig
from .db import connect, migrate
from .discovery import discover, failed_source_record, inspect_source
from .domain import (
    Classification,
    InspectionResult,
    InspectionStatus,
    SourceFormat,
    SourceRecord,
)
from .inspectors import inspect
from .parsing import classify
from .repositories import SourceRepository

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScanResult:
    source: SourceRecord
    inspection: InspectionResult
    classification: Classification


def scan(root: Path, config: AppConfig, *, persist: bool = True) -> list[ScanResult]:
    LOGGER.info("scan started root=%s persist=%s", root, persist)
    limits = ArchiveLimits(
        config.archive_entry_limit,
        config.archive_entry_size_limit,
        config.archive_total_size_limit,
        config.archive_path_depth_limit,
        config.archive_ratio_limit,
    )
    connection: sqlite3.Connection | None = None
    repository: SourceRepository | None = None
    if persist:
        if config.database_path is None:
            raise ValueError("database path is required when persistence is enabled")
        migrate(config.database_path)
        connection = connect(config.database_path)
        repository = SourceRepository(connection)
    results: list[ScanResult] = []
    try:
        for path in discover(root, config.excluded_roots()):
            try:
                source = inspect_source(path)
                inspection = inspect(path, source.format, limits)
            except Exception as exc:
                LOGGER.warning("inspection failed path=%s error=%s", path, exc)
                source = failed_source_record(path)
                inspection = InspectionResult(
                    InspectionStatus.FAILED,
                    SourceFormat.UNKNOWN,
                    error_code="source_inspection_error",
                    error_message=f"{type(exc).__name__}: {exc}",
                )
            classification = classify(path, source.format, inspection)
            result = ScanResult(source, inspection, classification)
            results.append(result)
            if repository is not None:
                source_id = repository.upsert(source)
                repository.add_inspection(source_id, inspection)
                repository.add_classification(source_id, classification)
        if connection is not None:
            connection.commit()
    finally:
        if connection is not None:
            connection.close()
    LOGGER.info("scan complete root=%s sources=%s", root, len(results))
    return results
