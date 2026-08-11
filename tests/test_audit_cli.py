from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from compatibility.helpers.epub_factory import create_epub
from kavita_ingest.audit import run_audit
from kavita_ingest.cli import app
from kavita_ingest.config import AppConfig
from kavita_ingest.domain import MediaKind
from kavita_ingest.providers.base import ProviderStatus
from kavita_ingest.providers.models import (
    Contributor,
    Identifier,
    NormalizedCandidate,
    ProviderName,
    RecordType,
    SearchQuery,
)


class FakeProvider:
    name = ProviderName.GOOGLE_BOOKS

    def __init__(self, candidate: NormalizedCandidate) -> None:
        self.candidate = candidate

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, False, False, "fixture", ("search",))

    def search(self, query: SearchQuery) -> list[NormalizedCandidate]:
        return [self.candidate]

    def fetch(self, provider_id: str) -> list[NormalizedCandidate]:
        return [self.candidate]

    def lookup_identifier(self, identifier: Identifier) -> list[NormalizedCandidate]:
        return [self.candidate]


def test_synthetic_audit_persists_candidates_but_no_approval(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    create_epub(incoming / "Fixture Book.epub")
    database = tmp_path / "state.sqlite3"
    candidate = NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        "fixture-volume",
        RecordType.BOOK_EDITION,
        media_kind=MediaKind.BOOK,
        title="Fixture Book",
        creators=(Contributor("Alex Author", "author"),),
    )
    result = run_audit(
        incoming,
        AppConfig(database_path=database),
        providers_override=(FakeProvider(candidate),),
    )
    assert result.summary["sources"] == 1
    assert result.summary["candidate_found"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM match_runs").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM match_candidates").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM decisions").fetchone() == (0,)


def test_offline_audit_command_reports_unresolved_without_network(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    create_epub(incoming / "Fixture Book.epub")
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[paths]
database = "{tmp_path / "state.sqlite3"}"
[providers]
offline = true
""",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["audit", str(incoming), "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "Unresolved:         1" in result.output
    assert "No identity was accepted" in result.output

    refused = CliRunner().invoke(app, ["plan", "create", str(incoming), "--config", str(config)])
    assert refused.exit_code == 2
    assert "No plan was created." in refused.output
    assert "Traceback" not in refused.output


def test_interactive_review_records_explicit_unresolved_decision(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    create_epub(incoming / "Fixture Book.epub")
    database = tmp_path / "state.sqlite3"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[paths]\ndatabase = "{database}"\n[providers]\noffline = true\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app, ["review", str(incoming), "--config", str(config)], input="U\n"
    )
    assert result.exit_code == 0, result.output
    assert "[S]earch" in result.output
    assert "[A]ccept" not in result.output
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT decision_type FROM decisions").fetchone() == (
            "unresolved",
        )


def test_doctor_reports_provider_credentials_without_live_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COMIC_VINE_API_KEY", raising=False)
    config = tmp_path / "config.toml"
    config.write_text("[providers]\noffline = true\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 0
    assert "COMIC_VINE_API_KEY is missing" in result.output
    assert "provider-live" in result.output
    assert str(config) in result.output


def test_provider_rate_configuration_rejects_unsafe_values(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[providers.comic_vine]\nmax_requests = 181\nmin_interval = 0.1\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code != 0
    assert "max_requests" in str(result.exception)
