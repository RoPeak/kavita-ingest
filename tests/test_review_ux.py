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
from kavita_ingest.candidates import CandidateGeneration, ProviderAttempt
from kavita_ingest.cli import app
from kavita_ingest.config import AppConfig, MatchingSettings, ProviderSettings
from kavita_ingest.db import connect, migrate
from kavita_ingest.decisions import DecisionRepository, DecisionType
from kavita_ingest.domain import MediaKind, SequenceNumber, SourceFormat, SourceRecord
from kavita_ingest.hydration import HydrationResult
from kavita_ingest.matching import LocalIdentity, reconcile, score_candidates
from kavita_ingest.providers.base import ProviderError, ProviderStatus
from kavita_ingest.providers.comic_vine import ComicVineProvider
from kavita_ingest.providers.models import (
    Contributor,
    NormalizedCandidate,
    ProviderName,
    RecordType,
)
from kavita_ingest.review import (
    _action_prompt,
    _batch_items,
    _candidate_writers,
    _choose_comic_vine_run,
    _collection_search_text,
    _current_group_fingerprints,
    _hydrate_for_acceptance,
    _mark_incompatible_group_decisions,
    interactive_review,
)
from kavita_ingest.run_groups import run_group_key


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


def test_review_filter_only_presents_requested_pending_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    base = _audit(tmp_path, eligible=True)
    items = []
    for index in range(3):
        source = replace(
            base.items[0].scan.source,
            path=tmp_path / f"Watchmen {index + 1:03}.cbz",
            sha256=f"{index + 1:064x}",
        )
        items.append(replace(base.items[0], scan=SimpleNamespace(source=source)))
    audit = replace(base, items=tuple(items), summary={"sources": 3})
    pending = frozenset({items[2].scan.source.sha256})
    monkeypatch.setattr(
        "kavita_ingest.review.typer.prompt", lambda *args, **kwargs: "U"
    )
    output = io.StringIO()

    interactive_review(
        tmp_path,
        AppConfig(database_path=database, providers=ProviderSettings(offline=True)),
        Console(file=output, force_terminal=False),
        audit_result=audit,
        wizard_mode=True,
        review_fingerprints=pending,
    )

    text = output.getvalue()
    assert "Watchmen 003.cbz" in text
    assert "Watchmen 001.cbz" not in text
    assert "Watchmen 002.cbz" not in text
    with connect(database) as connection:
        decisions = DecisionRepository(connection)
        assert decisions.latest(items[0].scan.source) is None
        assert decisions.latest(items[1].scan.source) is None
        latest = decisions.latest(items[2].scan.source)
        assert latest is not None and latest.decision_type is DecisionType.UNRESOLVED


def test_batch_scope_can_be_limited_to_pending_review_fingerprints(tmp_path: Path) -> None:
    base = _audit(tmp_path, eligible=True)
    items = []
    for index in range(3):
        source = replace(
            base.items[0].scan.source,
            path=tmp_path / f"Watchmen {index + 1:03}.cbz",
            sha256=f"{index + 10:064x}",
        )
        items.append(replace(base.items[0], scan=SimpleNamespace(source=source)))
    audit = replace(base, items=tuple(items), summary={"sources": 3})
    pending = frozenset({items[1].scan.source.sha256})

    eligible = _batch_items(audit, include_fingerprints=pending)

    assert [item[0].sha256 for item in eligible] == [items[1].scan.source.sha256]


def test_batch_items_and_prompt_use_same_eligible_subset(tmp_path: Path) -> None:
    audit = _audit(tmp_path, eligible=True)
    assert len(_batch_items(audit)) == 1
    assert "[B]atch" in _action_prompt(audit.items[0], audit)


def test_batch_items_can_exclude_source_already_decided_this_pass(tmp_path: Path) -> None:
    audit = _audit(tmp_path, eligible=True)
    fingerprint = audit.items[0].scan.source.sha256
    assert _batch_items(audit, exclude_fingerprints={fingerprint}) == []


def test_candidate_writer_summary_uses_search_credit_data() -> None:
    candidate = replace(
        _candidate("4000-writer", 2025, "2025-06-01"),
        creators=(
            Contributor("Al Ewing", "writer"),
            Contributor("Al Ewing", "writer"),
            Contributor("Jahnoy Lindsay", "artist"),
        ),
    )
    assert _candidate_writers(candidate) == "Al Ewing"


def test_wizard_review_uses_compact_primary_actions_and_more_reveals_advanced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    audit = _audit(tmp_path, eligible=True)
    compact = _action_prompt(audit.items[0], audit, wizard_mode=True)
    assert "[A] Accept" in compact and "[V] View why" in compact
    assert "[M] More actions" in compact
    assert "[E]dit" not in compact and "[G]roup-run" not in compact

    answers = iter(["M", "Q"])
    monkeypatch.setattr("kavita_ingest.review.typer.prompt", lambda *args, **kwargs: next(answers))
    output = io.StringIO()
    interactive_review(
        tmp_path,
        AppConfig(database_path=database, providers=ProviderSettings(offline=True)),
        Console(file=output, force_terminal=False),
        audit_result=audit,
        wizard_mode=True,
    )
    text = output.getvalue()
    assert "Advanced review actions are now available" in text
    advanced = _action_prompt(audit.items[0], audit)
    assert "[E]dit" in advanced and "[G]roup-run" in advanced


def test_compact_review_enter_has_no_next_default_and_reprompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    audit = _audit(tmp_path, eligible=True)
    answers = iter(["", "Q"])
    prompts: list[dict[str, object]] = []

    def prompt(*args: object, **kwargs: object) -> str:
        del args
        prompts.append(kwargs)
        return next(answers)

    monkeypatch.setattr("kavita_ingest.review.typer.prompt", prompt)
    output = io.StringIO()

    interactive_review(
        tmp_path,
        AppConfig(database_path=database, providers=ProviderSettings(offline=True)),
        Console(file=output, force_terminal=False),
        audit_result=audit,
        wizard_mode=True,
    )

    assert len(prompts) == 2
    assert all(call.get("default") is None for call in prompts)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM decisions").fetchone() == (0,)


def test_granular_review_retains_next_as_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    audit = _audit(tmp_path, eligible=True)
    defaults: list[object] = []

    def prompt(*args: object, **kwargs: object) -> str:
        del args
        defaults.append(kwargs.get("default"))
        return "N"

    monkeypatch.setattr("kavita_ingest.review.typer.prompt", prompt)
    interactive_review(
        tmp_path,
        AppConfig(database_path=database, providers=ProviderSettings(offline=True)),
        Console(file=io.StringIO(), force_terminal=False),
        audit_result=audit,
    )

    assert defaults == ["N"]


def test_wizard_review_labels_local_run_evidence_without_confusing_provider_data(
    tmp_path: Path,
) -> None:
    audit = _audit(tmp_path)
    output = io.StringIO()
    from kavita_ingest.review import _show_item

    _show_item(Console(file=output, force_terminal=False), audit.items[0], 1)
    text = output.getvalue()
    assert "Local run-start evidence: none" in text
    assert "start 1986" in text


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


class _DetailProvider:
    name = ProviderName.COMIC_VINE

    def __init__(self, detail: NormalizedCandidate | ProviderError) -> None:
        self.detail = detail

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, True, True, "fixture", ("exact_fetch",))

    def search(self, query: object) -> list[NormalizedCandidate]:
        del query
        return []

    def fetch(self, provider_id: str) -> list[NormalizedCandidate]:
        del provider_id
        if isinstance(self.detail, ProviderError):
            raise self.detail
        return [self.detail]

    def lookup_identifier(self, identifier: object) -> list[NormalizedCandidate]:
        del identifier
        return []


def test_collection_search_text_combines_series_and_collection_subtitle() -> None:
    local = LocalIdentity(
        MediaKind.COMIC,
        "collected-edition",
        0.98,
        "Ultimate Collection Book 1",
        creators=("Grant Morrison",),
        series_title="New X-Men",
        sequence=SequenceNumber.parse("1"),
    )

    assert _collection_search_text(local) == "New X-Men Ultimate Collection Book 1"

class _RunLookupClient:
    def get(
        self,
        operation: str,
        url: str,
        public_params: dict[str, str],
        secret_params: dict[str, str],
        bucket: str,
        normalize,  # type: ignore[no-untyped-def]
    ) -> list[NormalizedCandidate]:
        del url, public_params, secret_params, bucket
        assert operation == "search-runs"
        return normalize(
            {
                "results": [
                    {
                        "id": 53871,
                        "resource_type": "volume",
                        "api_detail_url": (
                            "https://comicvine.gamespot.com/api/volume/4050-53871/"
                        ),
                        "name": "Watchmen",
                        "start_year": None,
                        "publisher": {"name": "DC Comics"},
                    },
                    {
                        "id": 3622,
                        "resource_type": "volume",
                        "api_detail_url": (
                            "https://comicvine.gamespot.com/api/volume/4050-3622/"
                        ),
                        "name": "Watchmen",
                        "start_year": 1986,
                        "publisher": {"name": "DC Comics"},
                    },
                    {
                        "id": 29927,
                        "resource_type": "volume",
                        "api_detail_url": (
                            "https://comicvine.gamespot.com/api/volume/4050-29927/"
                        ),
                        "name": "Watchmen",
                        "start_year": 1987,
                        "publisher": {"name": "DC Comics"},
                    },
                ]
            }
        )


class _PollutedAliasRunLookupClient:
    def get(
        self,
        operation: str,
        url: str,
        public_params: dict[str, str],
        secret_params: dict[str, str],
        bucket: str,
        normalize,  # type: ignore[no-untyped-def]
    ) -> list[NormalizedCandidate]:
        del url, public_params, secret_params, bucket
        assert operation == "search-runs"
        return normalize(
            {
                "results": [
                    {
                        "id": 109114,
                        "resource_type": "volume",
                        "api_detail_url": (
                            "https://comicvine.gamespot.com/api/volume/4050-109114/"
                        ),
                        "name": "Oblivion Song",
                        "start_year": 2018,
                        "publisher": {"name": "Image"},
                    }
                ]
            }
        )


def test_group_run_chooser_uses_real_run_records_and_excludes_unknown_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = _audit(tmp_path)
    provider = ComicVineProvider(_RunLookupClient(), "secret")  # type: ignore[arg-type]
    monkeypatch.setattr("kavita_ingest.review.typer.prompt", lambda *args, **kwargs: 2)
    output = io.StringIO()

    selected = _choose_comic_vine_run(
        audit.items[0],
        (provider,),
        Console(file=output, force_terminal=False),
    )

    assert selected is not None
    assert selected.record_type is RecordType.COMIC_RUN
    assert selected.provider_id == "4050-29927"
    assert selected.run_start_year == 1987
    text = output.getvalue()
    assert "4050-53871" not in text
    assert "4050-3622" in text
    assert "4050-29927" in text


def test_group_run_chooser_keeps_candidate_carried_run_for_polluted_local_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _audit(tmp_path)
    item = base.items[0]
    local = replace(
        item.local,
        title="Chapter One",
        series_title="Oblivion Song By Kirkman & De Felici",
    )
    issue = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4000-700001",
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        "Chapter One",
        series_title="Oblivion Song",
        sequence=SequenceNumber.parse("1"),
        run_start_year=2018,
        publisher="Image",
        run_id="4050-109114",
    )
    scores = tuple(score_candidates(local, [issue], MatchingSettings()))
    polluted = replace(
        item,
        local=local,
        generation=CandidateGeneration((issue,), (), ()),
        scores=scores,
        reconciliation=reconcile(local, scores[0]),
    )
    provider = ComicVineProvider(
        _PollutedAliasRunLookupClient(), "secret"
    )  # type: ignore[arg-type]
    monkeypatch.setattr("kavita_ingest.review.typer.prompt", lambda *args, **kwargs: 1)
    output = io.StringIO()

    selected = _choose_comic_vine_run(
        polluted,
        (provider,),
        Console(file=output, force_terminal=False),
    )

    assert selected is not None
    assert selected.provider_id == "4050-109114"
    assert selected.series_title == "Oblivion Song"
    assert selected.run_start_year == 2018
    assert "4050-109114" in output.getvalue()


def test_group_run_choice_is_persisted_under_local_not_provider_series_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    base = _audit(tmp_path)
    item = base.items[0]
    local = replace(item.local, series_title="Oblivion Song By Kirkman & De Felici")
    audit = replace(base, items=(replace(item, local=local),))
    selected = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4050-109114",
        RecordType.COMIC_RUN,
        MediaKind.COMIC,
        "Oblivion Song",
        series_title="Oblivion Song",
        run_start_year=2018,
        publisher="Image",
        run_id="4050-109114",
    )
    monkeypatch.setattr("kavita_ingest.review._choose_comic_vine_run", lambda *args: selected)
    monkeypatch.setattr("kavita_ingest.review.typer.prompt", lambda *args, **kwargs: "G")
    monkeypatch.setattr("kavita_ingest.review.typer.confirm", lambda *args, **kwargs: True)

    interactive_review(
        tmp_path,
        AppConfig(database_path=database, providers=ProviderSettings(offline=True)),
        Console(file=io.StringIO(), force_terminal=False),
        audit_result=audit,
    )

    with connect(database) as connection:
        rows = [
            tuple(row)
            for row in connection.execute(
                "SELECT group_key, provider_run_id FROM run_group_decisions ORDER BY id"
            ).fetchall()
        ]
    assert rows == [
        (run_group_key("Oblivion Song By Kirkman & De Felici"), "4050-109114")
    ]


def test_run_group_change_append_only_marks_incompatible_issue_decision_unresolved(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    audit = _audit(tmp_path)
    item = audit.items[0]

    with connect(database) as connection:
        repository = DecisionRepository(connection)
        repository.add(
            item.scan.source,
            DecisionType.ACCEPTED,
            item.local.evidence_hash(),
            candidate_key=item.scores[0].candidate.key,
            candidate_data_hash=item.scores[0].candidate.data_hash(),
            payload={"candidate": item.scores[0].candidate.to_dict()},
        )

        changed = _mark_incompatible_group_decisions(
            repository,
            audit,
            run_group_key("Watchmen"),
            "4050-1987",
        )

        assert changed == 1
        history = repository.history(item.scan.source)
        assert [record.decision_type for record in history] == [
            DecisionType.ACCEPTED,
            DecisionType.UNRESOLVED,
        ]
        assert history[-1].payload["reason"] == "run_group_changed"
        assert history[-1].payload["selected_run_id"] == "4050-1987"

def test_successful_hydration_shows_enriched_creators_before_decision(
    tmp_path: Path,
) -> None:
    score = _audit(tmp_path).items[0].scores[0]
    detail = replace(
        score.candidate,
        creators=(
            Contributor("Scott Snyder", "writer"),
            Contributor("Nick Dragotta", "artist"),
        ),
    )
    output = io.StringIO()

    result = _hydrate_for_acceptance(
        score,
        (_DetailProvider(detail),),  # type: ignore[arg-type]
        Console(file=output, force_terminal=False),
    )

    assert result is not None and result[1].status == "hydrated"
    assert "Writer: Scott Snyder" in output.getvalue()
    assert "Artist: Nick Dragotta" in output.getvalue()


def test_failed_hydration_requires_explicit_sparse_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    score = _audit(tmp_path).items[0].scores[0]
    answers = iter([False, True])
    monkeypatch.setattr(
        "kavita_ingest.review.typer.confirm", lambda *args, **kwargs: next(answers)
    )
    output = io.StringIO()

    result = _hydrate_for_acceptance(
        score,
        (_DetailProvider(ProviderError("temporary outage")),),  # type: ignore[arg-type]
        Console(file=output, force_terminal=False),
    )

    assert result is not None
    assert isinstance(result[1], HydrationResult)
    assert result[1].status == "sparse_explicit"
    assert "remains unavailable" in output.getvalue()


def test_conflicting_exact_identity_cannot_be_accepted(tmp_path: Path) -> None:
    score = _audit(tmp_path).items[0].scores[0]
    detail = replace(score.candidate, sequence=SequenceNumber.parse("99"))
    output = io.StringIO()

    result = _hydrate_for_acceptance(
        score,
        (_DetailProvider(detail),),  # type: ignore[arg-type]
        Console(file=output, force_terminal=False),
    )

    assert result is None
    assert "issue number differs" in output.getvalue()
    assert "Not accepted" in output.getvalue()


def test_wizard_surfaces_group_run_and_hides_accept_when_run_year_is_missing(
    tmp_path: Path,
) -> None:
    audit = _audit(tmp_path, eligible=True)
    item = audit.items[0]
    missing_year = replace(item.scores[0].candidate, run_start_year=None)
    rescored = score_candidates(
        item.local,
        [missing_year],
        MatchingSettings(eligible_score=0, eligible_margin=0),
    )
    unresolved_item = replace(item, scores=tuple(rescored))

    prompt = _action_prompt(unresolved_item, audit, wizard_mode=True)

    assert "[G] Choose run" in prompt
    assert "[A] Accept" not in prompt
    assert rescored[0].eligible is False



def test_batch_defaults_to_current_series_group_and_keeps_global_explicit(tmp_path: Path) -> None:
    base = _audit(tmp_path, eligible=True)
    items = []
    for index, series in enumerate(("Watchmen", "Watchmen", "Saga", "Saga"), start=1):
        source = replace(
            base.items[0].scan.source,
            path=tmp_path / f"{series} {index:03}.cbz",
            sha256=f"{index + 100:064x}",
        )
        local = replace(base.items[0].local, title=series, series_title=series)
        candidate = replace(
            base.items[0].scores[0].candidate,
            title=series,
            series_title=series,
            provider_id=f"candidate-{index}",
            run_id=f"run-{series}",
        )
        score = replace(base.items[0].scores[0], candidate=candidate)
        items.append(
            replace(
                base.items[0],
                scan=SimpleNamespace(source=source),
                local=local,
                generation=CandidateGeneration((candidate,), (), ()),
                scores=(score,),
                reconciliation=reconcile(local, score),
            )
        )
    audit = replace(base, items=tuple(items), summary={"sources": 4})

    current = items[0]
    group = _current_group_fingerprints(current, audit, None)

    assert group == frozenset(
        {items[0].scan.source.sha256, items[1].scan.source.sha256}
    )
    prompt = _action_prompt(current, audit)
    assert "[B]atch current group (2)" in prompt
    assert "[Y] batch entire pending ingest (4)" in prompt


def test_invalid_review_action_reprompts_without_redrawing_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    audit = _audit(tmp_path, eligible=True)
    answers = iter(["Z", "U"])
    monkeypatch.setattr(
        "kavita_ingest.review.typer.prompt", lambda *args, **kwargs: next(answers)
    )
    output = io.StringIO()

    interactive_review(
        tmp_path,
        AppConfig(database_path=database, providers=ProviderSettings(offline=True)),
        Console(file=output, force_terminal=False),
        audit_result=audit,
        wizard_mode=True,
    )

    text = output.getvalue()
    assert text.count("Path:") == 1
    assert "Choose one of the displayed actions." in text


def test_no_match_diagnostics_explain_provider_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    migrate(database)
    audit = _unusable_audit(tmp_path)
    item = replace(
        audit.items[0],
        generation=replace(
            audit.items[0].generation,
            attempts=(
                ProviderAttempt(
                    ProviderName.OPEN_LIBRARY,
                    "collection:structured",
                    "ok",
                    raw_count=3,
                    accepted_count=0,
                    rejection_counts=(("collection_sequence_conflict", 3),),
                ),
                ProviderAttempt(
                    ProviderName.COMIC_VINE,
                    "collection-format",
                    "skipped",
                    detail="unsupported_collection_format",
                ),
            ),
        ),
    )
    audit = replace(audit, items=(item,))
    answers = iter(["V", "U"])
    monkeypatch.setattr(
        "kavita_ingest.review.typer.prompt", lambda *args, **kwargs: next(answers)
    )
    output = io.StringIO()

    interactive_review(
        tmp_path,
        AppConfig(database_path=database, providers=ProviderSettings(offline=True)),
        Console(file=output, force_terminal=False),
        audit_result=audit,
        wizard_mode=True,
    )

    text = output.getvalue()
    assert "[V] Why no matches?" in _action_prompt(item, audit, wizard_mode=True)
    assert "3 returned, 0 usable" in text
    assert "collection sequence" in text and "conflict" in text
    assert "provider cannot prove collection" in text and "format" in text
