from __future__ import annotations

import importlib.resources
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: tuple[int, ...]
    backup_path: Path | None


def connect(path: Path, *, enable_wal: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if enable_wal:
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


def migrate(path: Path) -> MigrationResult:
    existed = path.exists() and path.stat().st_size > 0
    scripts = _migration_scripts()
    backup: Path | None = None
    with connect(path, enable_wal=False) as connection:
        migration_table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        applied = (
            {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}
            if migration_table_exists
            else set()
        )
        pending = [(version, sql) for version, sql in scripts if version not in applied]
        if existed and pending:
            backup = _validated_backup(connection, path)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.commit()
        connection.execute("PRAGMA journal_mode = WAL")
        completed: list[int] = []
        for version, sql in pending:
            applied_at = _now().replace("'", "''")
            try:
                connection.executescript(
                    f"BEGIN IMMEDIATE;\n{sql}\n"
                    "INSERT INTO schema_migrations(version, applied_at) "
                    f"VALUES ({version}, '{applied_at}');\nCOMMIT;"
                )
            except sqlite3.Error:
                connection.rollback()
                raise
            completed.append(version)
        connection.commit()
    return MigrationResult(tuple(completed), backup)


def _migration_scripts() -> list[tuple[int, str]]:
    root = importlib.resources.files("kavita_ingest").joinpath("migrations")
    scripts: list[tuple[int, str]] = []
    for item in root.iterdir():
        match = re.fullmatch(r"(\d{4})_[a-z0-9_]+\.sql", item.name)
        if match:
            scripts.append((int(match.group(1)), item.read_text(encoding="utf-8")))
    return sorted(scripts)


def _validated_backup(connection: sqlite3.Connection, path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.backup-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.backup-{stamp}-{suffix}")
        suffix += 1
    with sqlite3.connect(candidate) as backup:
        connection.backup(backup)
    with sqlite3.connect(candidate) as check:
        result = check.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        candidate.unlink(missing_ok=True)
        raise RuntimeError("database backup failed integrity validation")
    return candidate


def _now() -> str:
    return datetime.now(UTC).isoformat()
