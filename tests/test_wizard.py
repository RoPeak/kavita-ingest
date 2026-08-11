from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kavita_ingest.apply_engine import ApplyEngine, InjectedCrash
from kavita_ingest.cli import app
from kavita_ingest.db import connect
from kavita_ingest.plan_store import PlanStore
from kavita_ingest.wizard import detect_resume_state
from tests.apply_helpers import ApplyFixture, make_apply_fixture


def _config(path: Path, fixture: ApplyFixture) -> Path:
    config = path / "wizard.toml"
    config.write_text(
        f'''[paths]
database = "{fixture.config.database_path}"
incoming = ["{fixture.source.parent}"]
books = "{fixture.config.books_root}"
comics = "{fixture.config.comics_root}"

[source]
lifecycle = "preserve"

[providers]
offline = true

[providers.comic_vine]
enabled = false
''',
        encoding="utf-8",
    )
    return config


def test_wizard_resumes_draft_and_binds_full_digest_without_applying(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve", approve=False)
    config = _config(tmp_path, fixture)

    result = CliRunner().invoke(app, ["wizard", "--config", str(config)], input="y\ny\nn\n")

    assert result.exit_code == 0, result.output
    assert "Resume available" in result.output
    assert "full digest was bound internally" in result.output
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        plan = PlanStore(connection).get(fixture.plan_id)
    assert plan.status == "approved" and plan.approval_digest == plan.sha256
    assert fixture.source.exists() and not fixture.destination.exists()


def test_wizard_resumes_approved_plan_without_reapproval(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    result = CliRunner().invoke(
        app, ["wizard", "--config", str(_config(tmp_path, fixture))], input="y\nn\n"
    )
    assert result.exit_code == 0, result.output
    assert "Approve this exact" not in result.output
    assert fixture.source.exists() and not fixture.destination.exists()


@pytest.mark.parametrize(("media_format", "work_only"), [("cbz", False), ("epub", True)])
def test_wizard_applies_approved_comic_and_work_only_epub_with_production_engine(
    media_format: str, work_only: bool, tmp_path: Path
) -> None:
    fixture = make_apply_fixture(
        tmp_path, media_format, lifecycle="preserve", work_only=work_only
    )
    result = CliRunner().invoke(
        app, ["wizard", "--config", str(_config(tmp_path, fixture))], input="y\ny\n"
    )
    assert result.exit_code == 0, result.output
    assert "Status: complete" in result.output
    assert fixture.source.exists() and fixture.destination.exists()


def test_wizard_makes_recovery_prominent_and_uses_existing_engine(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")

    def crash(checkpoint: str, item_id: str) -> None:
        del item_id
        if checkpoint == "after_verified_journal":
            raise InjectedCrash(checkpoint)

    with pytest.raises(InjectedCrash):
        ApplyEngine(fixture.config, fault=crash).apply(fixture.plan_id)
    result = CliRunner().invoke(
        app, ["wizard", "--config", str(_config(tmp_path, fixture))], input="y\ny\n"
    )
    assert result.exit_code == 0, result.output
    assert "incomplete apply" in result.output and "Status: complete" in result.output
    assert fixture.destination.exists()


def test_resume_detection_ignores_invalidated_plan(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve", approve=False)
    database = fixture.config.database_path
    assert database is not None
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO plan_invalidations(plan_id, reason, invalidated_at) VALUES (?, ?, ?)",
            (fixture.plan_id, "decision changed", "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
    assert detect_resume_state(fixture.config) is None


def test_wizard_explains_provider_unavailable_and_saves_unresolved_decision(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    books = tmp_path / "books"
    comics = tmp_path / "comics"
    for directory in (incoming, books, comics):
        directory.mkdir()
    with zipfile.ZipFile(incoming / "Unknown Series 001.cbz", "w") as archive:
        archive.writestr("001.jpg", b"page")
    config = tmp_path / "unavailable.toml"
    config.write_text(
        f'''[paths]
database = "{tmp_path / 'state.sqlite3'}"
incoming = ["{incoming}"]
books = "{books}"
comics = "{comics}"

[source]
lifecycle = "preserve"

[providers]
offline = true

[providers.open_library]
enabled = false
[providers.google_books]
enabled = false
[providers.comic_vine]
enabled = false
''',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app, ["wizard", "--config", str(config)], input="y\ny\nu\n"
    )

    assert result.exit_code == 0, result.output
    assert "Provider availability affected" in result.output
    assert "No plan was created" in result.output
    assert "Review remains saved" in result.output
