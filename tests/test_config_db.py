from __future__ import annotations

import importlib.resources
import sqlite3
from pathlib import Path

import pytest

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


def test_missing_toml_still_loads_provider_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMIC_VINE_API_KEY", "comic-secret")
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", "google-secret")
    monkeypatch.setenv("KAVITA_INGEST_OPEN_LIBRARY_CONTACT", "contact@example.test")

    config = load_config(app_paths=_paths(tmp_path))

    assert config.database_path == tmp_path / "state/state.sqlite3"
    assert config.providers.comic_vine_api_key == "comic-secret"
    assert config.providers.google_books_api_key == "google-secret"
    assert config.providers.open_library_contact == "contact@example.test"


def test_new_database_applies_numbered_migration_without_backup(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    result = migrate(database)
    assert result.applied == (1, 2, 3, 4)
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
    assert result.applied == (1, 2, 3, 4)
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


def test_provider_migration_backs_up_version_one_before_schema_change(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migration = (
        importlib.resources.files("kavita_ingest")
        .joinpath("migrations/0001_initial.sql")
        .read_text(encoding="utf-8")
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.executescript(migration)
        connection.execute("INSERT INTO schema_migrations VALUES (1, 'fixture')")
    result = migrate(database)
    assert result.applied == (2, 3, 4)
    assert result.backup_path is not None
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
        assert (
            backup.execute("SELECT name FROM sqlite_master WHERE name='provider_cache'").fetchone()
            is None
        )


def test_planning_migration_backs_up_version_two_before_schema_change(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migrations = importlib.resources.files("kavita_ingest").joinpath("migrations")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version, filename in (
            (1, "0001_initial.sql"),
            (2, "0002_providers_matching_decisions.sql"),
        ):
            connection.executescript(migrations.joinpath(filename).read_text(encoding="utf-8"))
            connection.execute("INSERT INTO schema_migrations VALUES (?, 'fixture')", (version,))
    result = migrate(database)
    assert result.applied == (3, 4)
    assert result.backup_path is not None
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("SELECT version FROM schema_migrations").fetchall() == [(1,), (2,)]
        assert (
            backup.execute("SELECT name FROM sqlite_master WHERE name='plans'").fetchone() is None
        )


def test_apply_migration_backs_up_version_three_before_journal_schema(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    migrations = importlib.resources.files("kavita_ingest").joinpath("migrations")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version, filename in (
            (1, "0001_initial.sql"),
            (2, "0002_providers_matching_decisions.sql"),
            (3, "0003_run_groups_and_plans.sql"),
        ):
            connection.executescript(migrations.joinpath(filename).read_text(encoding="utf-8"))
            connection.execute("INSERT INTO schema_migrations VALUES (?, 'fixture')", (version,))
    result = migrate(database)
    assert result.applied == (4,)
    assert result.backup_path is not None
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute(
            "SELECT name FROM sqlite_master WHERE name='apply_runs'"
        ).fetchone() is None
