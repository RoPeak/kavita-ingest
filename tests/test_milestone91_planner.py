from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from compatibility.helpers.epub_factory import create_epub
from kavita_ingest.apply_engine import ApplyEngine, ApplyRefused
from kavita_ingest.cli import app
from kavita_ingest.comicinfo import read_comicinfo
from kavita_ingest.config import load_config
from kavita_ingest.db import connect
from kavita_ingest.decisions import DecisionRepository, DecisionType
from kavita_ingest.discovery import inspect_source
from kavita_ingest.domain import MediaKind
from kavita_ingest.plan_store import PlanStore
from kavita_ingest.providers.base import ProviderStatus
from kavita_ingest.providers.comic_vine import ComicVineProvider
from kavita_ingest.providers.models import (
    Contributor,
    Identifier,
    NormalizedCandidate,
    ProviderName,
    RecordType,
    SearchQuery,
)
from kavita_ingest.run_groups import RunGroupRepository


class _BookWorkProvider:
    name = ProviderName.GOOGLE_BOOKS

    def __init__(self) -> None:
        self.candidate = NormalizedCandidate(
            ProviderName.GOOGLE_BOOKS,
            "fixture-work",
            RecordType.BOOK_WORK,
            MediaKind.BOOK,
            "Fixture Book",
            creators=(Contributor("Alex Author", "author"),),
            work_id="fixture-work",
        )

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, False, False, "fixture", ("search",))

    def search(self, query: SearchQuery) -> list[NormalizedCandidate]:
        return [self.candidate]

    def fetch(self, provider_id: str) -> list[NormalizedCandidate]:
        return [self.candidate]

    def lookup_identifier(self, identifier: Identifier) -> list[NormalizedCandidate]:
        return [self.candidate]


class _ComicVineFixtureClient:
    def get(
        self,
        operation: str,
        url: str,
        public_params: dict[str, str],
        secret_params: dict[str, str],
        bucket: str,
        normalize: Callable[[object], list[NormalizedCandidate]],
    ) -> list[NormalizedCandidate]:
        del url, public_params, secret_params, bucket
        if operation == "search-runs":
            payload: object = {
                "results": [
                    {
                        "id": 160294,
                        "resource_type": "volume",
                        "api_detail_url": (
                            "https://comicvine.gamespot.com/api/volume/4050-160294/"
                        ),
                        "name": "Absolute Batman",
                        "start_year": "2024",
                        "publisher": {"name": "DC Comics"},
                    }
                ]
            }
        else:
            payload = json.loads(
                Path("tests/fixtures/providers/comic_vine.json").read_text(encoding="utf-8")
            )
        return normalize(payload)


class _IncrementalComicVineFixtureClient:
    def get(
        self,
        operation: str,
        url: str,
        public_params: dict[str, str],
        secret_params: dict[str, str],
        bucket: str,
        normalize: Callable[[object], list[NormalizedCandidate]],
    ) -> list[NormalizedCandidate]:
        del url, secret_params, bucket
        if operation == "search-runs":
            return normalize(
                {
                    "results": [
                        {
                            "id": identifier,
                            "resource_type": "volume",
                            "api_detail_url": (
                                f"https://comicvine.gamespot.com/api/volume/4050-{identifier}/"
                            ),
                            "name": "Absolute Batman",
                            "start_year": str(year),
                            "publisher": {"name": "DC Comics"},
                        }
                        for identifier, year in ((160294, 2024), (260294, 2026), (360294, 1989))
                    ]
                }
            )
        volume = int(public_params["filter"].split(",", 1)[0].split(":", 1)[1])
        if volume == 360294:
            return normalize({"results": []})
        payload = json.loads(
            Path("tests/fixtures/providers/comic_vine.json").read_text(encoding="utf-8")
        )
        if volume == 260294:
            payload["results"][0]["id"] = 240001
            payload["results"][0]["api_detail_url"] = (
                "https://comicvine.gamespot.com/api/issue/4000-240001/"
            )
            payload["results"][0]["cover_date"] = "2027-04-01"
            payload["results"][0]["volume"]["id"] = 260294
            payload["results"][0]["volume"]["start_year"] = "2026"
            payload["results"][0]["volume"]["api_detail_url"] = (
                "https://comicvine.gamespot.com/api/volume/4050-260294/"
            )
        return normalize(payload)


def _config(
    tmp_path: Path,
    incoming: Path,
    *,
    lifecycle: str = "move_after_verify",
    cbr_conversion: bool = True,
    padding: int = 3,
) -> Path:
    books = tmp_path / "books"
    comics = tmp_path / "comics"
    archive = tmp_path / "archive"
    for path in (books, comics, archive):
        path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f'''[paths]
database = "{tmp_path / "state.sqlite3"}"
incoming = ["{incoming}"]
books = "{books}"
comics = "{comics}"

[source]
lifecycle = "{lifecycle}"
archive_root = "{archive}"

[cbr]
convert_to_cbz = {str(cbr_conversion).lower()}

[naming]
comic_specials_subfolder = true

[sequence]
integer_padding = {padding}

[providers]
offline = true

[providers.comic_vine]
enabled = false
''',
        encoding="utf-8",
    )
    return config


def _cbz(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", b"page-one")
        archive.writestr("002.jpg", b"page-two")
    return path


def _create_approve_apply(
    runner: CliRunner,
    config: Path,
    incoming: Path,
    review_input: str,
    before_apply: Callable[[], None] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    scanned = runner.invoke(app, ["scan", str(incoming), "--config", str(config)])
    assert scanned.exit_code == 0, scanned.output
    reviewed = runner.invoke(
        app,
        ["review", str(incoming), "--config", str(config)],
        input=review_input,
    )
    assert reviewed.exit_code == 0, reviewed.output
    created = runner.invoke(
        app,
        ["plan", "create", str(incoming), "--json", "--config", str(config)],
    )
    assert created.exit_code == 0, created.output
    created_payload = json.loads(created.stdout)
    plan_id = str(created_payload["plan_id"])
    digest = str(created_payload["sha256"])
    shown = runner.invoke(
        app,
        ["plan", "show", plan_id, "--json", "--config", str(config)],
    )
    assert shown.exit_code == 0, shown.output
    document = json.loads(shown.stdout)["document"]
    approved = runner.invoke(
        app,
        ["plan", "approve", plan_id, "--digest", digest, "--config", str(config)],
    )
    assert approved.exit_code == 0, approved.output
    if before_apply is not None:
        before_apply()
    applied = runner.invoke(
        app,
        ["apply", plan_id, "--json", "--config", str(config)],
    )
    assert applied.exit_code == 0, applied.output
    return document, json.loads(applied.stdout)


def test_generated_epub_runs_through_real_scan_review_plan_and_apply(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = create_epub(incoming / "Fixture Book.epub")
    config = _config(tmp_path, incoming)

    document, applied = _create_approve_apply(
        CliRunner(),
        config,
        incoming,
        "I\nResolved Book\nAlex Author\n\n\n\n\n\n\n",
    )

    item = document["items"][0]
    destination = tmp_path / "books" / "Resolved Book" / "Resolved Book.epub"
    assert document["schema_version"] == 2
    assert item["planning_policy"] == document["planning_policy"]
    assert item["expected_inventory"]
    assert item["provenance"]["decision_type"] == "manual_identity"
    assert applied["summary"]["status"] == "complete"
    assert destination.is_file()
    assert not source.exists()


def test_book_work_normal_accept_is_confirmed_and_persisted_work_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = create_epub(incoming / "Fixture Book.epub")
    config = _config(tmp_path, incoming, lifecycle="preserve")
    provider = (_BookWorkProvider(),)
    monkeypatch.setattr("kavita_ingest.audit.build_providers", lambda *_: provider)
    monkeypatch.setattr("kavita_ingest.review.build_providers", lambda *_: provider)

    document, applied = _create_approve_apply(CliRunner(), config, incoming, "A\ny\ny\n")

    item = document["items"][0]
    ownership = item["ownership_manifest"]
    destination = tmp_path / "books" / "Fixture Book" / "Fixture Book.epub"
    assert item["partial_resolution"]["level"] == "work_only"
    assert item["provenance"]["decision_type"] == "work_accepted"
    assert set(ownership["preserve"]) >= {
        "publisher",
        "date",
        "language",
        "identifiers",
    }
    assert applied["summary"]["status"] == "complete"
    assert source.is_file() and destination.is_file()


def test_generated_cbz_freezes_symbolic_naming_and_preserve_policy(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = _cbz(incoming / "Watchmen 1A (1986).cbz")
    config = _config(tmp_path, incoming, lifecycle="preserve", padding=5)

    document, applied = _create_approve_apply(
        CliRunner(),
        config,
        incoming,
        "I\nWatchmen\nAt Midnight\nissue\n1A\n1986\n",
    )

    item = document["items"][0]
    projection = item["kavita_projection"]
    destination = tmp_path / "comics" / projection["destination"]
    assert projection["metadata"]["Series"] == "Watchmen (1986)"
    assert projection["metadata"]["Number"] == "1A"
    assert "1A" in projection["filename"]
    assert item["planning_policy"]["naming"]["integer_padding"] == 5
    assert item["lifecycle_actions"][-1]["action"] == "preserve"
    assert item["expected_inventory"]
    assert applied["summary"]["status"] == "complete"
    assert source.is_file() and destination.is_file()


def test_comic_vine_candidate_survives_real_review_plan_apply_and_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = _cbz(incoming / "Absolute Batman 14 (2024).cbz")
    config = _config(tmp_path, incoming, lifecycle="preserve")
    provider = (ComicVineProvider(_ComicVineFixtureClient(), "fixture-key"),)  # type: ignore[arg-type]
    monkeypatch.setattr("kavita_ingest.audit.build_providers", lambda *_: provider)
    monkeypatch.setattr("kavita_ingest.review.build_providers", lambda *_: provider)

    document, applied = _create_approve_apply(CliRunner(), config, incoming, "A\ny\n")

    item = document["items"][0]
    canonical = item["canonical"]
    projection = item["kavita_projection"]
    metadata = projection["metadata"]
    destination = tmp_path / "comics" / projection["destination"]
    assert item["provenance"]["decision_type"] == "accepted"
    assert canonical["provider_identity"]["run_id"] == "4050-160294"
    assert canonical["run_start_year"] == 2024
    assert canonical["sequence"]["normalized"] == "14"
    assert canonical["title"] == "The Zoo"
    assert canonical["contributors"]["writers"] == ["Scott Snyder"]
    assert metadata["Series"] == "Absolute Batman (2024)"
    assert metadata["Number"] == "14"
    assert metadata["Title"] == "The Zoo"
    assert metadata["Writer"] == "Scott Snyder"
    assert metadata["Publisher"] == "DC Comics"
    assert (metadata["Year"], metadata["Month"], metadata["Day"]) == (2026, 1, 15)
    assert applied["summary"]["status"] == "complete"
    assert source.is_file() and destination.is_file()
    with zipfile.ZipFile(destination) as archive:
        comicinfo = read_comicinfo(archive.read("ComicInfo.xml"), require_schema=True).metadata
    assert comicinfo["Writer"] == "Scott Snyder"
    assert comicinfo["Publisher"] == "DC Comics"
    assert (comicinfo["Year"], comicinfo["Month"], comicinfo["Day"]) == (
        "2026",
        "1",
        "15",
    )


def test_incremental_absolute_batman_14_cli_journey_creates_plan_without_issue_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = _cbz(incoming / "Absolute Batman 014 (2026) (Digital) (Shan-Empire).cbz")
    config = _config(tmp_path, incoming, lifecycle="preserve")
    provider = (
        ComicVineProvider(_IncrementalComicVineFixtureClient(), "fixture-key"),  # type: ignore[arg-type]
    )
    monkeypatch.setattr("kavita_ingest.audit.build_providers", lambda *_: provider)
    monkeypatch.setattr("kavita_ingest.review.build_providers", lambda *_: provider)
    runner = CliRunner()

    scanned = runner.invoke(app, ["scan", str(incoming), "--config", str(config)])
    assert scanned.exit_code == 0, scanned.output
    audited = runner.invoke(app, ["audit", str(incoming), "--details", "--config", str(config)])
    assert audited.exit_code == 0, audited.output
    assert "Absolute Batman #14; run 2024; 2026-01-15" in audited.output
    assert "INFO kavita_ingest" not in audited.output

    reviewed = runner.invoke(app, ["review", str(incoming), "--config", str(config)], input="A\n")
    assert reviewed.exit_code == 0, reviewed.output
    assert "4050-160294" in reviewed.output
    assert "comic_run" not in reviewed.output
    assert "Decision saved." in reviewed.output

    created = runner.invoke(
        app,
        ["plan", "create", str(incoming), "--json", "--config", str(config)],
    )
    assert created.exit_code == 0, created.output
    payload = json.loads(created.stdout)
    shown = runner.invoke(
        app,
        ["plan", "show", str(payload["plan_id"]), "--json", "--config", str(config)],
    )
    document = json.loads(shown.stdout)["document"]
    item = document["items"][0]
    assert item["canonical"]["provider_identity"]["run_id"] == "4050-160294"
    assert item["canonical"]["run_start_year"] == 2024
    assert item["canonical"]["sequence"]["normalized"] == "14"
    assert item["lifecycle_actions"][-1]["action"] == "preserve"
    assert source.is_file()


@pytest.mark.skipif(shutil.which("unrar") is None, reason="unrar is required for CBR workflow")
def test_cbr_conversion_policy_is_frozen_by_real_planner(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "Watchmen 1 (1986).cbr"
    shutil.copy2("compatibility/fixtures/rar/rar5-subdirs.rar", source)
    config = _config(tmp_path, incoming)

    def tighten_mutable_config() -> None:
        config.write_text(
            config.read_text(encoding="utf-8") + "\n[archive]\nmax_entries = 1\n",
            encoding="utf-8",
        )

    document, applied = _create_approve_apply(
        CliRunner(),
        config,
        incoming,
        "I\nWatchmen\nAt Midnight\nissue\n1\n1986\n",
        tighten_mutable_config,
    )

    item = document["items"][0]
    destination = tmp_path / "comics" / item["kavita_projection"]["destination"]
    assert item["transformations"] == [{"type": "cbr_to_cbz"}]
    assert item["planning_policy"]["cbr_conversion_enabled"] is True
    assert item["planning_policy"]["archive_limits"]["max_entries"] > 0
    assert load_config(config).archive_entry_limit == 1
    assert item["expected_inventory"]
    assert destination.suffix == ".cbz" and destination.is_file()
    assert applied["summary"]["status"] == "complete"
    assert not source.exists()


def test_plan_create_refuses_cbr_when_conversion_policy_is_disabled(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source = incoming / "Watchmen 1 (1986).cbr"
    shutil.copy2("compatibility/fixtures/rar/rar5-subdirs.rar", source)
    config = _config(tmp_path, incoming, cbr_conversion=False)
    runner = CliRunner()
    assert runner.invoke(app, ["scan", str(incoming), "--config", str(config)]).exit_code == 0
    reviewed = runner.invoke(
        app,
        ["review", str(incoming), "--config", str(config)],
        input="I\nWatchmen\nAt Midnight\nissue\n1\n1986\n",
    )
    assert reviewed.exit_code == 0, reviewed.output

    refused = runner.invoke(
        app,
        ["plan", "create", str(incoming), "--config", str(config)],
    )

    assert refused.exit_code == 2
    assert "CBR conversion is disabled" in refused.output
    assert source.is_file()


def test_new_identity_decision_invalidates_approved_plan_before_apply(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source_path = create_epub(incoming / "Fixture Book.epub")
    config_path = _config(tmp_path, incoming, lifecycle="preserve")
    runner = CliRunner()
    assert runner.invoke(app, ["scan", str(incoming), "--config", str(config_path)]).exit_code == 0
    reviewed = runner.invoke(
        app,
        ["review", str(incoming), "--config", str(config_path)],
        input="I\nResolved Book\nAlex Author\n\n\n\n\n\n\n",
    )
    assert reviewed.exit_code == 0, reviewed.output
    created = runner.invoke(
        app,
        ["plan", "create", str(incoming), "--json", "--config", str(config_path)],
    )
    payload = json.loads(created.stdout)
    plan_id = int(payload["plan_id"])
    approved = runner.invoke(
        app,
        [
            "plan",
            "approve",
            str(plan_id),
            "--digest",
            payload["sha256"],
            "--config",
            str(config_path),
        ],
    )
    assert approved.exit_code == 0, approved.output
    settings = load_config(config_path)
    with connect(settings.database_path) as connection:  # type: ignore[arg-type]
        DecisionRepository(connection).add(
            inspect_source(source_path),
            DecisionType.UNRESOLVED,
            "new-evidence",
            payload={"explicit": True},
        )
        invalidation = connection.execute(
            "SELECT reason FROM plan_invalidations WHERE plan_id=?", (plan_id,)
        ).fetchone()
    assert invalidation and "identity decision changed" in invalidation[0]
    with pytest.raises(ApplyRefused, match="invalidated"):
        ApplyEngine(settings).apply(plan_id)
    assert source_path.is_file()


def test_new_draft_supersedes_older_unapplied_plan_for_same_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    create_epub(incoming / "Fixture Book.epub")
    config = _config(tmp_path, incoming, lifecycle="preserve")
    runner = CliRunner()
    assert runner.invoke(app, ["scan", str(incoming), "--config", str(config)]).exit_code == 0
    reviewed = runner.invoke(
        app,
        ["review", str(incoming), "--config", str(config)],
        input="I\nResolved Book\nAlex Author\n\n\n\n\n\n\n",
    )
    assert reviewed.exit_code == 0, reviewed.output

    def provider_forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("plan creation must not construct providers")

    monkeypatch.setattr("kavita_ingest.provider_runtime.build_providers", provider_forbidden)
    monkeypatch.setattr("kavita_ingest.audit.build_providers", provider_forbidden)
    monkeypatch.setattr("kavita_ingest.review.build_providers", provider_forbidden)
    first = json.loads(
        runner.invoke(
            app,
            ["plan", "create", str(incoming), "--json", "--config", str(config)],
        ).stdout
    )
    second = json.loads(
        runner.invoke(
            app,
            ["plan", "create", str(incoming), "--json", "--config", str(config)],
        ).stdout
    )
    with connect(load_config(config).database_path) as connection:  # type: ignore[arg-type]
        row = connection.execute(
            "SELECT new_plan_id FROM plan_supersessions WHERE old_plan_id=?",
            (first["plan_id"],),
        ).fetchone()
        with pytest.raises(ValueError, match="superseded plan"):
            PlanStore(connection).approve(int(first["plan_id"]), str(first["sha256"]))
    assert row and row[0] == second["plan_id"]


def test_run_group_change_invalidates_comic_plan(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _cbz(incoming / "Watchmen 1 (1986).cbz")
    config = _config(tmp_path, incoming, lifecycle="preserve")
    runner = CliRunner()
    assert runner.invoke(app, ["scan", str(incoming), "--config", str(config)]).exit_code == 0
    reviewed = runner.invoke(
        app,
        ["review", str(incoming), "--config", str(config)],
        input="I\nWatchmen\nAt Midnight\nissue\n1\n1986\n",
    )
    assert reviewed.exit_code == 0, reviewed.output
    settings = load_config(config)
    with connect(settings.database_path) as connection:  # type: ignore[arg-type]
        groups = RunGroupRepository(connection)
        groups.choose(
            "comic:watchmen",
            "comic_vine",
            "4050-1986",
            {"provider_id": "4050-1986", "title": "Watchmen", "run_start_year": 1986},
        )
    created = runner.invoke(
        app,
        ["plan", "create", str(incoming), "--json", "--config", str(config)],
    )
    assert created.exit_code == 0, created.output
    plan_id = json.loads(created.stdout)["plan_id"]
    with connect(settings.database_path) as connection:  # type: ignore[arg-type]
        RunGroupRepository(connection).choose(
            "comic:watchmen",
            "comic_vine",
            "4050-2024",
            {"provider_id": "4050-2024", "title": "Watchmen", "run_start_year": 2024},
        )
        invalidation = connection.execute(
            "SELECT reason FROM plan_invalidations WHERE plan_id=?", (plan_id,)
        ).fetchone()
    assert invalidation and "run-group decision changed" in invalidation[0]
