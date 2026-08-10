from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path
from typing import Never

import pytest
from typer.testing import CliRunner

from compatibility.helpers.epub_factory import create_epub
from kavita_ingest import scanner
from kavita_ingest.cli import app
from kavita_ingest.config import AppConfig
from kavita_ingest.scanner import scan


def test_scan_persists_successes_and_failures_without_mutating_sources(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    epub = create_epub(incoming / "Fixture Book.epub")
    bad_cbz = incoming / "Broken Comic.cbz"
    with zipfile.ZipFile(bad_cbz, "w") as archive:
        archive.writestr("../escape.jpg", b"payload")
    before = {path: path.read_bytes() for path in (epub, bad_cbz)}
    database = tmp_path / "state.sqlite3"
    results = scan(incoming, AppConfig(database_path=database))
    assert len(results) == 2
    assert {item.inspection.status.value for item in results} == {"ok", "failed"}
    assert {path: path.read_bytes() for path in before} == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM sources").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM inspections").fetchone() == (2,)
        assert connection.execute(
            "SELECT error_code FROM inspections WHERE status='failed'"
        ).fetchone() == ("invalid_cbz",)


def test_cli_read_only_scan_and_doctor(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    create_epub(incoming / "Fixture Book.epub")
    runner = CliRunner()
    scan_result = runner.invoke(app, ["scan", str(incoming), "--no-persist"])
    doctor_result = runner.invoke(app, ["doctor"])
    assert scan_result.exit_code == 0, scan_result.output
    assert "book" in scan_result.output
    assert "source files were not modified" in scan_result.output
    assert doctor_result.exit_code == 0
    assert "pikepdf" in doctor_result.output


def test_scan_persists_per_file_source_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "Changing.pdf").write_bytes(b"%PDF-1.4\n")
    database = tmp_path / "state.sqlite3"

    def fail(_path: Path) -> Never:
        raise OSError("source changed while it was being fingerprinted")

    monkeypatch.setattr(scanner, "inspect_source", fail)
    results = scanner.scan(incoming, AppConfig(database_path=database))
    assert results[0].inspection.error_code == "source_inspection_error"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM inspections").fetchone() == ("failed",)
