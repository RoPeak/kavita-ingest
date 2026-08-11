from __future__ import annotations

import io
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from kavita_ingest.audit import AuditResult, ReviewItem
from kavita_ingest.candidates import CandidateGeneration
from kavita_ingest.cli import app
from kavita_ingest.config import AppConfig, MatchingSettings, ProviderSettings
from kavita_ingest.db import migrate
from kavita_ingest.domain import MediaKind, SequenceNumber, SourceFormat, SourceRecord
from kavita_ingest.matching import LocalIdentity, reconcile, score_candidates
from kavita_ingest.providers.models import NormalizedCandidate, ProviderName, RecordType
from kavita_ingest.review import _action_prompt, _batch_items, interactive_review


def _candidate(identifier: str, year: int, date: str) -> NormalizedCandidate:
    return NormalizedCandidate(
        ProviderName.COMIC_VINE,
        identifier,
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        f"Issue from {year}",
        series_title="Watchmen",
        sequence=SequenceNumber.parse("1"),
        run_start_year=year,
        cover_date=date[:7],
        cover_date_precision="month",
        run_id=f"4050-{year}",
    )


def _audit(tmp_path: Path, *, eligible: bool = False) -> AuditResult:
    source = SourceRecord(
        tmp_path / "Watchmen 001.cbz", 10, 1, "a" * 64, SourceFormat.CBZ, "zip-cbz"
    )
    local = LocalIdentity(
        MediaKind.COMIC,
        "issue",
        0.98,
        "Watchmen",
        series_title="Watchmen",
        sequence=SequenceNumber.parse("1"),
    )
    settings = (
        MatchingSettings(eligible_score=0, eligible_margin=0) if eligible else MatchingSettings()
    )
    scores = score_candidates(
        local,
        [
            _candidate("4000-first", 1986, "1986-09-01"),
            _candidate("4000-second", 2024, "2024-01-01"),
        ],
        settings,
    )
    item = ReviewItem(
        SimpleNamespace(source=source),
        local,
        CandidateGeneration(tuple(score.candidate for score in scores), (), ()),
        tuple(scores),
        reconcile(local, scores[0]),
    )
    return AuditResult((item,), {"sources": 1}, 1, {}, {})


def _unusable_audit(tmp_path: Path) -> AuditResult:
    audit = _audit(tmp_path)
    item = audit.items[0]
    run = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4050-context",
        RecordType.COMIC_RUN,
        MediaKind.COMIC,
        "Watchmen",
        series_title="Watchmen",
        run_start_year=1986,
    )
    run_score = score_candidates(item.local, [run], MatchingSettings())[0]
    zero_score = replace(item.scores[0], score=0, eligible=False)
    unusable = replace(
        item,
        generation=CandidateGeneration((run, zero_score.candidate), (), ()),
        scores=(run_score, zero_score),
    )
    summary = {
        "sources": 1,
        "eligible_high_confidence": 0,
        "review_required": 1,
        "unresolved": 0,
        "provider_unavailable": 0,
        "partial_provider_unavailable": 0,
    }
    return AuditResult((unusable,), summary, 1, {}, {})


def test_rank_two_can_be_selected_and_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    audit = _audit(tmp_path)
    monkeypatch.setattr("kavita_ingest.review.run_audit", lambda *args, **kwargs: audit)
    answers = iter(["2", "A"])
    monkeypatch.setattr("kavita_ingest.review.typer.prompt", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr("kavita_ingest.review.typer.confirm", lambda *args, **kwargs: True)
    output = io.StringIO()

    interactive_review(
        tmp_path,
        AppConfig(database_path=database, providers=ProviderSettings(offline=True)),
        Console(file=output, force_terminal=False),
    )

    with sqlite3.connect(database) as connection:
        payload = connection.execute("SELECT payload_json FROM decisions").fetchone()[0]
    assert "4000-second" in payload
    assert "Decision saved." in output.getvalue()


def test_declined_low_confidence_acceptance_saves_nothing_and_summarizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    audit = _audit(tmp_path)
    monkeypatch.setattr("kavita_ingest.review.run_audit", lambda *args, **kwargs: audit)
    answers = iter(["A", "N"])
    monkeypatch.setattr("kavita_ingest.review.typer.prompt", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr("kavita_ingest.review.typer.confirm", lambda *args, **kwargs: False)
    output = io.StringIO()

    interactive_review(
        tmp_path,
        AppConfig(database_path=database, providers=ProviderSettings(offline=True)),
        Console(file=output, force_terminal=False),
    )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM decisions").fetchone() == (0,)
    text = output.getvalue()
    assert "Not accepted; no decision was saved." in text
    assert "No decision: 1" in text
    assert "[N]ext source" in _action_prompt(audit.items[0], audit)
    assert "[W]ork-only" not in _action_prompt(audit.items[0], audit)


def test_batch_items_and_prompt_use_same_eligible_subset(tmp_path: Path) -> None:
    audit = _audit(tmp_path, eligible=True)
    assert len(_batch_items(audit)) == 1
    assert "[B]atch" in _action_prompt(audit.items[0], audit)


def test_search_with_only_unusable_candidates_stays_operable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    audit = _audit(tmp_path)
    unusable = _unusable_audit(tmp_path).items[0]
    monkeypatch.setattr("kavita_ingest.review.run_audit", lambda *args, **kwargs: audit)
    monkeypatch.setattr(
        "kavita_ingest.review.generate_candidates",
        lambda *args, **kwargs: CandidateGeneration((unusable.generation.candidates[0],), (), ()),
    )
    answers = iter(["S", "Revised Watchmen", "N"])
    monkeypatch.setattr("kavita_ingest.review.typer.prompt", lambda *args, **kwargs: next(answers))
    output = io.StringIO()

    interactive_review(
        tmp_path,
        AppConfig(database_path=database, providers=ProviderSettings(offline=True)),
        Console(file=output, force_terminal=False),
    )

    assert "No useful identity candidates are available." in output.getvalue()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM decisions").fetchone() == (0,)


def test_candidate_actions_are_hidden_without_a_usable_candidate(tmp_path: Path) -> None:
    audit = _unusable_audit(tmp_path)

    prompt = _action_prompt(audit.items[0], audit)

    for action in ("[A]ccept", "[R]eject", "[C]hoose", "e[X]plain", "[G]roup-run"):
        assert action not in prompt
    for action in ("[S]earch", "[E]dit", "[I]dentity", "[U]nresolved", "[K]skip"):
        assert action in prompt


def test_audit_details_reports_unresolved_when_only_candidates_are_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    config = tmp_path / "config.toml"
    config.write_text("[providers]\noffline = true\n", encoding="utf-8")
    audit = _unusable_audit(tmp_path)
    monkeypatch.setattr("kavita_ingest.cli.run_audit", lambda *args, **kwargs: audit)

    result = CliRunner().invoke(app, ["audit", str(incoming), "--details", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert f"{audit.items[0].scan.source.path.name}: unresolved" in result.output
    assert "comic_run" not in result.output
