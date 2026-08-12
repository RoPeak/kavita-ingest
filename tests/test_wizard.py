from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from kavita_ingest.apply_engine import ApplyEngine, InjectedCrash
from kavita_ingest.cli import app
from kavita_ingest.completed_sources import assess_completed_sources
from kavita_ingest.config import AppConfig, load_config
from kavita_ingest.db import connect, migrate
from kavita_ingest.decisions import DecisionRepository, DecisionType, add_manual_identity
from kavita_ingest.plan_store import PlanStore
from kavita_ingest.planning_service import PlanBuilder
from kavita_ingest.scanner import scan
from kavita_ingest.wizard import (
    DiscoverySelection,
    _incomplete_review_items,
    _select_discovered_sources,
    _select_root,
    detect_resume_state,
)
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

    result = CliRunner().invoke(app, ["wizard", "--config", str(config)], input="r\na\nn\n")

    assert result.exit_code == 0, result.output
    assert "A draft plan is ready to review" in result.output
    assert "exact displayed plan digest is now locked" in result.output
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        plan = PlanStore(connection).get(fixture.plan_id)
    assert plan.status == "approved" and plan.approval_digest == plan.sha256
    assert fixture.source.exists() and not fixture.destination.exists()


def test_wizard_resumes_approved_plan_without_reapproval(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    result = CliRunner().invoke(
        app, ["wizard", "--config", str(_config(tmp_path, fixture))], input="r\nn\n"
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
        app, ["wizard", "--config", str(_config(tmp_path, fixture))], input="r\ny\nq\n"
    )
    assert result.exit_code == 0, result.output
    assert "Ingest complete" in result.output
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
        app, ["wizard", "--config", str(_config(tmp_path, fixture))], input="r\ny\nq\n"
    )
    assert result.exit_code == 0, result.output
    assert "interrupted ingest" in result.output and "Ingest complete" in result.output
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
        app, ["wizard", "--config", str(config)], input="\nu\nq\n"
    )

    assert result.exit_code == 0, result.output
    assert "Provider problems" in result.output
    assert "Review is incomplete" in result.output
    assert "Review decisions saved" in result.output


def test_reviewed_decision_without_plan_resumes_directly_to_offline_planning(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    books = tmp_path / "books"
    comics = tmp_path / "comics"
    for directory in (incoming, books, comics):
        directory.mkdir()
    with zipfile.ZipFile(incoming / "Watchmen 001.cbz", "w") as archive:
        archive.writestr("001.jpg", b"page")
    config = tmp_path / "reviewed.toml"
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
    reviewed = CliRunner().invoke(
        app,
        ["review", str(incoming), "--config", str(config)],
        input="i\nWatchmen\nAt Midnight\nissue\n1\n1986\n",
    )
    assert reviewed.exit_code == 0, reviewed.output
    state = detect_resume_state(load_config(config))
    assert state is not None and state.kind == "reviewed" and state.item_count == 1

    resumed = CliRunner().invoke(
        app, ["wizard", "--config", str(config)], input="r\nq\n"
    )
    assert resumed.exit_code == 0, resumed.output
    assert "providers will not be queried again" in resumed.output
    assert "Draft plan saved" in resumed.output
    with connect(tmp_path / "state.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM plans").fetchone()[0] == 1


def test_fresh_wizard_has_one_start_action_and_full_human_review_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve", approve=False)
    config = _config(tmp_path, fixture)
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        stored = PlanStore(connection).get(fixture.plan_id)
    audit = SimpleNamespace(
        items=(object(),),
        summary={
            "sources": 1,
            "eligible_high_confidence": 1,
            "review_required": 0,
            "unresolved": 0,
            "provider_unavailable": 0,
            "partial_provider_unavailable": 0,
        },
    )
    build = SimpleNamespace(
        accepted_included=1,
        unapproved_excluded=0,
        unresolved_blocked=0,
        skipped=0,
        exclusions=(),
    )
    monkeypatch.setattr("kavita_ingest.wizard.detect_resume_state", lambda _: None)
    monkeypatch.setattr("kavita_ingest.wizard._preflight", lambda *args: None)
    monkeypatch.setattr("kavita_ingest.wizard.run_audit", lambda *args, **kwargs: audit)
    monkeypatch.setattr("kavita_ingest.wizard.interactive_review", lambda *args, **kwargs: audit)
    monkeypatch.setattr("kavita_ingest.wizard._incomplete_review_items", lambda *args, **kwargs: ())
    monkeypatch.setattr("kavita_ingest.wizard._create_plan", lambda *args: (stored, build))

    result = CliRunner().invoke(
        app,
        ["wizard", "--config", str(config)],
        input="\nv\nt\na\ny\nq\n",
    )

    assert result.exit_code == 0, result.output
    assert "Start a new guided ingest?" not in result.output
    assert "Use configured incoming root" not in result.output
    assert "Ready to confirm     1" in result.output
    assert "Metadata and output" in result.output
    assert "Full SHA-256" in result.output
    assert "exact displayed plan digest is now locked" in result.output
    assert "Ingest complete" in result.output
    assert fixture.source.exists() and fixture.destination.exists()


def test_human_status_summarizes_last_ingest_and_next_action(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    ApplyEngine(fixture.config).apply(fixture.plan_id)

    result = CliRunner().invoke(
        app, ["status", "--config", str(_config(tmp_path, fixture))]
    )

    assert result.exit_code == 0, result.output
    assert "Last ingest" in result.output
    assert "1 item completed" in result.output
    assert "No recovery required" in result.output
    assert "Draft plans          0" in result.output
    assert "Approved plans       0" in result.output
    assert "Start a new guided ingest" in result.output


def test_disposable_fresh_wizard_smoke_publishes_with_safe_mode(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    books = tmp_path / "books"
    comics = tmp_path / "comics"
    for directory in (incoming, books, comics):
        directory.mkdir()
    source = incoming / "Watchmen 001.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.jpg", b"page-one")
        archive.writestr("002.jpg", b"page-two")
    config = tmp_path / "smoke.toml"
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
        app,
        ["wizard", "--config", str(config)],
        input="\ni\nWatchmen\nAt Midnight\nissue\n1\n1986\nv\na\ny\nq\n",
    )

    destination = comics / "Watchmen (1986)" / "Watchmen (1986) - 001 - At Midnight.cbz"
    assert result.exit_code == 0, result.output
    assert "[1/7] Preflight" in result.output and "[7/7] Finish" in result.output
    assert "Metadata and output" in result.output and "Ingest complete" in result.output
    assert source.exists() and destination.exists()
    assert destination.stat().st_mode & 0o777 == 0o644

    second = CliRunner().invoke(
        app,
        ["wizard", "--config", str(config)],
        input="\nq\n",
    )
    assert second.exit_code == 0, second.output
    assert "Already ingested    1" in second.output
    assert "Nothing new to ingest" in second.output
    assert "[N] New/change source" in second.output
    assert "[3/7] Review" not in second.output

    reprocess = CliRunner().invoke(
        app,
        ["wizard", "--config", str(config)],
        input="\nr\nq\nq\n",
    )
    assert reprocess.exit_code == 0, reprocess.output
    assert "[3/7] Review" in reprocess.output
    assert "Review is incomplete" in reprocess.output
    assert destination.exists()


def test_review_completion_gate_blocks_missing_but_allows_explicit_unresolved(
    tmp_path: Path,
) -> None:
    from tests.test_review_ux import _audit

    database = tmp_path / "state.sqlite3"
    migrate(database)
    audit = _audit(tmp_path, eligible=True)
    settings = AppConfig(database_path=database)
    assert len(_incomplete_review_items(settings, audit)) == 1

    with connect(database) as connection:
        DecisionRepository(connection).add(
            audit.items[0].scan.source,
            DecisionType.UNRESOLVED,
            audit.items[0].local.evidence_hash(),
            payload={"explicit": True},
        )
    assert _incomplete_review_items(settings, audit) == ()

    with connect(database) as connection:
        DecisionRepository(connection).add(
            audit.items[0].scan.source,
            DecisionType.SKIPPED,
            audit.items[0].local.evidence_hash(),
            payload={"explicit": True},
        )
    assert _incomplete_review_items(settings, audit) == ()



def test_select_root_reprompts_after_nonexistent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid = tmp_path / "valid"
    valid.mkdir()
    answers = iter([str(tmp_path / "missing"), str(valid)])
    monkeypatch.setattr(
        "kavita_ingest.wizard.typer.prompt", lambda *args, **kwargs: next(answers)
    )

    assert _select_root(AppConfig()) == valid.resolve()


def test_completed_preserved_source_is_suppressed_only_with_matching_destination(
    tmp_path: Path,
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    ApplyEngine(fixture.config).apply(fixture.plan_id)
    scans = scan(fixture.source.parent, fixture.config, persist=True)
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        assessment = assess_completed_sources(connection, scans)

    assert not assessment.current
    assert len(assessment.completed) == 1
    assert assessment.completed[0].destination == fixture.destination


def test_changed_completed_source_is_current_work(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    ApplyEngine(fixture.config).apply(fixture.plan_id)
    with zipfile.ZipFile(fixture.source, "a") as archive:
        archive.writestr("003.jpg", b"changed-page")
    scans = scan(fixture.source.parent, fixture.config, persist=True)
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        assessment = assess_completed_sources(connection, scans)

    assert len(assessment.current) == 1
    assert not assessment.completed and not assessment.warnings


@pytest.mark.parametrize("condition", ["missing", "mismatch"])
def test_completed_source_with_invalid_destination_returns_to_current_work(
    condition: str, tmp_path: Path
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    ApplyEngine(fixture.config).apply(fixture.plan_id)
    if condition == "missing":
        fixture.destination.unlink()
    else:
        fixture.destination.write_bytes(b"changed destination")
    scans = scan(fixture.source.parent, fixture.config, persist=True)
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        assessment = assess_completed_sources(connection, scans)

    assert len(assessment.current) == 1 and not assessment.completed
    assert assessment.warnings[0].condition == f"destination_{condition}"


@pytest.mark.parametrize("lifecycle", ["move_after_verify", "archive_after_verify"])
def test_completed_moved_or_archived_source_is_not_rediscovered(
    lifecycle: str, tmp_path: Path
) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle=lifecycle)
    ApplyEngine(fixture.config).apply(fixture.plan_id)

    assert scan(fixture.source.parent, fixture.config, persist=True) == []


def test_explicit_reprocess_selection_returns_to_review_and_requires_new_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_review_ux import _audit

    audit = _audit(tmp_path, eligible=True)
    completed = SimpleNamespace(
        current=(),
        completed=(SimpleNamespace(scan=audit.items[0].scan, destination=tmp_path / "out.cbz"),),
        warnings=(),
    )
    monkeypatch.setattr("kavita_ingest.wizard._choice", lambda default: "R")

    selection = _select_discovered_sources(completed, Console(file=io.StringIO()))

    assert selection == DiscoverySelection((audit.items[0].scan,), reprocess=True)

    database = tmp_path / "state.sqlite3"
    migrate(database)
    settings = AppConfig(database_path=database)
    with connect(database) as connection:
        first = DecisionRepository(connection).add(
            audit.items[0].scan.source,
            DecisionType.SKIPPED,
            audit.items[0].local.evidence_hash(),
            payload={"explicit": True},
        )
    baseline = {audit.items[0].scan.source.sha256: first.id}
    assert len(_incomplete_review_items(settings, audit, required_newer_than=baseline)) == 1

    with connect(database) as connection:
        DecisionRepository(connection).add(
            audit.items[0].scan.source,
            DecisionType.SKIPPED,
            audit.items[0].local.evidence_hash(),
            payload={"explicit": True},
        )
    assert _incomplete_review_items(settings, audit, required_newer_than=baseline) == ()


def test_reprocess_plan_preserves_destination_no_clobber_conflict(tmp_path: Path) -> None:
    fixture = make_apply_fixture(tmp_path, "cbz", lifecycle="preserve")
    ApplyEngine(fixture.config).apply(fixture.plan_id)
    scanned = scan(fixture.source.parent, fixture.config, persist=True)[0]
    with connect(fixture.config.database_path) as connection:  # type: ignore[arg-type]
        add_manual_identity(
            DecisionRepository(connection),
            scanned.source,
            "explicit-reprocess-evidence",
            {
                "series_title": "Watchmen",
                "title": "At Midnight",
                "item_type": "issue",
                "sequence": "1",
                "run_start_year": "1986",
            },
        )
        result = PlanBuilder(connection, fixture.config).build(fixture.source.parent)

    assert result.conflicts == 1
    assert result.document.items[0].conflicts[0].code == "destination_exists"
    assert fixture.destination.exists()
