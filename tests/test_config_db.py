from __future__ import annotations

import sqlite3
from pathlib import Path

from kavita_ingest.config import load_config
from kavita_ingest.db import connect, migrate
from kavita_ingest.paths import AppPaths


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root / "config",
        root / "data",
        root / "state",
        root / "cache",
        root / "config/config.toml",
        root / "state/state.sqlite3",
        root / "state/app.log",
    )


def test_load_config_uses_toml_and_platform_default(tmp_path: Path) -> None:
    config_file = tmp_path / "settings.toml"
    config_file.write_text(
        """
[paths]
incoming = ["~/Incoming"]
books = "/srv/books"
comics = "/srv/comics"
staging = "/srv/staging"
ignore = ["/srv/ignore"]
[archive]
max_entries = 42
[logging]
level = "debug"
""",
        encoding="utf-8",
    )
    config = load_config(config_file, _paths(tmp_path))
    assert config.archive_entry_limit == 42
    assert config.log_level == "DEBUG"
    assert config.incoming_roots[0] == Path("~/Incoming").expanduser()
    assert {
        Path("/srv/books"),
        Path("/srv/comics"),
        Path("/srv/staging"),
        Path("/srv/ignore"),
    } == set(config.excluded_roots())


def test_new_database_applies_numbered_migration_without_backup(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    result = migrate(database)
    assert result.applied == (1,)
    assert result.backup_path is None
    with connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"schema_migrations", "sources", "inspections", "classifications"} <= tables


def test_existing_unmigrated_database_is_backed_up_and_validated(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE existing(value TEXT)")
        connection.execute("INSERT INTO existing VALUES ('preserve me')")
    result = migrate(database)
    assert result.applied == (1,)
    assert result.backup_path is not None and result.backup_path.exists()
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT value FROM existing").fetchone() == ("preserve me",)
        assert (
            backup.execute("SELECT name FROM sqlite_master WHERE name='sources'").fetchone() is None
        )
        assert (
            backup.execute(
                "SELECT name FROM sqlite_master WHERE name='schema_migrations'"
            ).fetchone()
            is None
        )


def test_current_database_does_not_create_redundant_backup(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    result = migrate(database)
    assert result.applied == ()
    assert result.backup_path is None
