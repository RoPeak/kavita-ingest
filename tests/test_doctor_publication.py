from __future__ import annotations

import errno
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kavita_ingest.apply_engine import ApplyEngine, StalePlan
from kavita_ingest.cli import app
from kavita_ingest.doctor import _path_check
from kavita_ingest.filesystem import LinuxFilesystem
from tests.apply_helpers import make_apply_fixture


def _config(tmp_path: Path) -> Path:
    books = tmp_path / "books"
    comics = tmp_path / "comics"
    incoming = tmp_path / "incoming"
    for path in (books, comics, incoming):
        path.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'''[paths]
database = "{tmp_path / "state.sqlite3"}"
incoming = ["{incoming}"]
books = "{books}"
comics = "{comics}"

[providers]
offline = true

[providers.comic_vine]
enabled = false
''',
        encoding="utf-8",
    )
    return config


def test_doctor_empirically_probes_no_clobber_without_leaving_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = CliRunner().invoke(app, ["doctor", "--json", "--config", str(config)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    publication = [check for check in payload["checks"] if check["category"] == "publication"]
    assert publication and all(check["status"] == "OK" for check in publication)
    assert not list(tmp_path.rglob(".kavita-ingest-doctor-*"))
    serialized = json.dumps(payload)
    assert "api_key" not in serialized.casefold()


def test_doctor_reports_effective_planning_policy_and_anonymous_provider_constraint(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    result = CliRunner().invoke(app, ["doctor", "--json", "--config", str(config)])
    assert result.exit_code == 0, result.output
    checks = json.loads(result.stdout)["checks"]
    planning = {item["name"]: item for item in checks if item["category"] == "planning"}
    assert planning["source-lifecycle"]["detail"].startswith("move_after_verify")
    assert "CBR -> CBZ enabled" in planning["cbr-conversion"]["detail"]
    assert "integer_padding=3" in planning["naming"]["detail"]
    assert "entries=5000" in planning["archive-limits"]["detail"]
    google = next(item for item in checks if item["name"] == "google-books")
    assert google["status"] == "INFO"
    assert google["detail"] == "anonymous access"


def test_doctor_warns_when_space_is_low_for_archive_repacking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = shutil._ntuple_diskusage(10 * 1024**3, 9 * 1024**3, 1024**3)
    monkeypatch.setattr("kavita_ingest.doctor.shutil.disk_usage", lambda _: usage)

    result = _path_check("paths", "comics", tmp_path, 4 * 1024**3)

    assert result.status == "WARN"
    assert "large CBR repacks may be blocked by apply preflight" in result.detail


def test_publication_probe_blocks_filesystems_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    monkeypatch.setattr("kavita_ingest.filesystem.os.link", unsupported)
    result = LinuxFilesystem().probe_no_clobber(tmp_path)
    assert not result.supported
    assert "hard-link publication probe failed" in result.detail


def test_apply_preflight_blocks_before_staging_when_hard_links_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz")

    def unsupported(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    monkeypatch.setattr("kavita_ingest.filesystem.os.link", unsupported)
    with pytest.raises(StalePlan, match="hard-link publication probe failed"):
        ApplyEngine(fixture.config).apply(fixture.plan_id)
    assert fixture.source.exists()
    assert not fixture.destination.exists()


def test_doctor_blocks_missing_calibre_for_epub_and_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kavita_ingest.doctor.shutil.which",
        lambda _: None,
    )

    from kavita_ingest.doctor import (
        _format_checks,
        _helper_checks,
    )

    helpers = {check.name: check for check in _helper_checks()}
    formats = {check.name: check for check in _format_checks()}

    assert helpers["ebook-meta"].status == "BLOCKED"
    assert "not found" in helpers["ebook-meta"].detail
    assert formats["EPUB"].status == "BLOCKED"
    assert formats["PDF"].status == "BLOCKED"


def test_doctor_blocks_unsafe_calibre_for_epub_and_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kavita_ingest.doctor.shutil.which",
        lambda executable: "/opt/calibre/ebook-meta" if executable == "ebook-meta" else None,
    )

    def unsafe(_: str) -> str:
        raise ValueError(
            "unsafe Calibre version: 9.11.0; "
            "kavita-ingest requires calibre >= 9.12.0 "
            "for untrusted EPUB/PDF metadata"
        )

    monkeypatch.setattr(
        "kavita_ingest.doctor.require_safe_calibre_executable",
        unsafe,
    )

    from kavita_ingest.doctor import (
        _format_checks,
        _helper_checks,
    )

    helpers = {check.name: check for check in _helper_checks()}
    formats = {check.name: check for check in _format_checks()}

    assert helpers["ebook-meta"].status == "BLOCKED"
    assert "9.11.0" in helpers["ebook-meta"].detail
    assert formats["EPUB"].status == "BLOCKED"
    assert formats["PDF"].status == "BLOCKED"


def test_doctor_accepts_safe_calibre_for_epub_and_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kavita_ingest.doctor.shutil.which",
        lambda executable: "/opt/calibre/ebook-meta" if executable == "ebook-meta" else None,
    )

    monkeypatch.setattr(
        "kavita_ingest.doctor.require_safe_calibre_executable",
        lambda _: "9.13",
    )

    from kavita_ingest.doctor import (
        _format_checks,
        _helper_checks,
    )

    helpers = {check.name: check for check in _helper_checks()}
    formats = {check.name: check for check in _format_checks()}

    assert helpers["ebook-meta"].status == "OK"
    assert "9.13" in helpers["ebook-meta"].detail
    assert "9.12.0" in helpers["ebook-meta"].detail
    assert formats["EPUB"].status == "OK"
    assert formats["PDF"].status == "OK"
    assert "verified Calibre XMP" in formats["PDF"].detail
