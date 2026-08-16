from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from kavita_ingest.cli import app
from kavita_ingest.db import connect, migrate
from kavita_ingest.decisions import DecisionRepository, DecisionType
from kavita_ingest.discovery import inspect_source


def _config(tmp_path: Path, incoming: Path, library: Path, database: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        f'''[paths]
database = "{database}"
incoming = ["{incoming}"]
books = "{library / 'Books'}"
comics = "{library / 'Comics'}"

[providers]
offline = true
''',
        encoding="utf-8",
    )
    return config


def _seed_completed_publication(database: Path, destination: Path) -> None:
    payload = b'{"schema_version":3,"items":[],"planning_policy":{},"conflicts":[]}'
    digest = hashlib.sha256(payload).hexdigest()
    destination_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
    timestamp = "2026-08-15T00:00:00+00:00"
    with connect(database) as connection:
        plan_id = connection.execute(
            "INSERT INTO plans(schema_version, canonical_json, byte_length, sha256, status, "
            "created_at, approved_at, approval_digest) VALUES (3, ?, ?, ?, 'approved', ?, ?, ?)",
            (payload, len(payload), digest, timestamp, timestamp, digest),
        ).lastrowid
        assert plan_id is not None
        connection.execute(
            "INSERT INTO apply_runs(id, plan_id, plan_digest, status, started_at, updated_at, "
            "completed_at) VALUES ('run', ?, ?, 'complete', ?, ?, ?)",
            (plan_id, digest, timestamp, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO apply_items(run_id, item_id, state, source_path, planned_source_hash, "
            "destination_path, destination_hash, lifecycle_policy, started_at, updated_at, "
            "completed_at) VALUES ('run', 'item', 'complete', '/old/source.cbr', ?, ?, ?, "
            "'remove_after_verify', ?, ?, ?)",
            ("a" * 64, str(destination), destination_hash, timestamp, timestamp, timestamp),
        )
        connection.commit()


def test_reset_published_cli_supports_relative_comics_target(tmp_path: Path) -> None:
    incoming = tmp_path / "Incoming" / "Reading"
    comics_incoming = incoming / "Comics"
    library = tmp_path / "Libraries" / "Kavita"
    comics_library = library / "Comics" / "Saga" / "Specials"
    for directory in (comics_incoming, comics_library, library / "Books"):
        directory.mkdir(parents=True)
    database = tmp_path / "state.sqlite3"
    migrate(database)
    destination = comics_library / "Saga - 001 - Saga.cbz"
    destination.write_bytes(b"verified-published-output")
    _seed_completed_publication(database, destination)
    config = _config(tmp_path, incoming, library, database)

    result = CliRunner().invoke(
        app,
        [
            "reset-published",
            str(destination),
            "--to",
            "Comics/Saga, Vol. 1 (2012).cbz",
            "--yes",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.output
    assert not destination.exists()
    assert (comics_incoming / "Saga, Vol. 1 (2012).cbz").read_bytes() == (
        b"verified-published-output"
    )
    assert "Historical apply journal preserved" in result.output


def test_reopen_review_cli_marks_existing_unresolved_source_pending(tmp_path: Path) -> None:
    incoming = tmp_path / "Incoming" / "Reading"
    comics_incoming = incoming / "Comics"
    library = tmp_path / "Libraries" / "Kavita"
    for directory in (comics_incoming, library / "Books", library / "Comics"):
        directory.mkdir(parents=True)
    source_path = comics_incoming / "Saga v04 (2014).cbz"
    with zipfile.ZipFile(source_path, "w") as archive:
        archive.writestr("001.jpg", b"page")
    database = tmp_path / "state.sqlite3"
    migrate(database)
    source = inspect_source(source_path)
    with connect(database) as connection:
        DecisionRepository(connection).add(
            source,
            DecisionType.UNRESOLVED,
            "evidence",
            payload={"explicit": True},
        )
    config = _config(tmp_path, incoming, library, database)

    result = CliRunner().invoke(
        app,
        ["reopen-review", str(source_path), "--yes", "--config", str(config)],
    )

    assert result.exit_code == 0, result.output
    with connect(database) as connection:
        latest = DecisionRepository(connection).latest(source)
    assert latest is not None
    assert latest.payload["reason"] == "review_reopened"
    assert "Source bytes were not modified" in result.output
