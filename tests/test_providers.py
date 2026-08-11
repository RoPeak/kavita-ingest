from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from kavita_ingest.domain import MediaKind, SequenceNumber
from kavita_ingest.providers.comic_vine import ComicVineProvider
from kavita_ingest.providers.google_books import GoogleBooksProvider
from kavita_ingest.providers.models import Identifier, NormalizedCandidate, RecordType, SearchQuery
from kavita_ingest.providers.open_library import OpenLibraryProvider

FIXTURES = Path("tests/fixtures/providers")


class FixtureClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str], dict[str, str], str]] = []

    def get(
        self,
        operation: str,
        url: str,
        public_params: dict[str, str],
        secret_params: dict[str, str],
        bucket: str,
        normalize: Callable[[object], list[NormalizedCandidate]],
    ) -> list[NormalizedCandidate]:
        self.calls.append((operation, public_params, secret_params, bucket))
        return normalize(self.payload)


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_open_library_normalizes_work_and_edition_and_isbn_lookup() -> None:
    client = FixtureClient(_fixture("open_library_edition.json"))
    provider = OpenLibraryProvider(client, "contact@example.test")  # type: ignore[arg-type]
    candidates = provider.lookup_identifier(Identifier("isbn13", "9780140449136"))
    assert [item.record_type for item in candidates] == [RecordType.BOOK_EDITION]
    assert candidates[0].work_id == "works/OL123W"
    assert candidates[0].edition_id == "OL456M"
    assert candidates[0].identifiers[-1] == Identifier("isbn", "9780140449136")
    assert client.calls[0][0] == "isbn"
    assert provider.status().detail == "identified contact mode"


def test_open_library_unidentified_mode_is_available() -> None:
    provider = OpenLibraryProvider(FixtureClient(_fixture("open_library.json")), None)  # type: ignore[arg-type]
    assert provider.status().enabled is True
    assert "unidentified" in provider.status().detail


def test_open_library_title_search_returns_work_not_synthetic_edition() -> None:
    provider = OpenLibraryProvider(FixtureClient(_fixture("open_library.json")), None)  # type: ignore[arg-type]
    candidates = provider.search(SearchQuery(MediaKind.BOOK, "Crime and Punishment"))
    assert [item.record_type for item in candidates] == [RecordType.BOOK_WORK]


def test_google_books_normalizes_edition_fields() -> None:
    client = FixtureClient(_fixture("google_books.json"))
    provider = GoogleBooksProvider(client)  # type: ignore[arg-type]
    candidate = provider.search(SearchQuery(MediaKind.BOOK, "The Odyssey", creators=("Homer",)))[0]
    assert candidate.record_type is RecordType.BOOK_EDITION
    assert candidate.publisher == "Fixture Classics"
    assert candidate.publication_date == "2020-05-01"
    assert candidate.identifiers == (Identifier("isbn_13", "9780140268867"),)
    assert "inauthor" in client.calls[0][1]["q"]


def test_comic_vine_normalizes_run_issue_without_kavita_volume_conflation() -> None:
    client = FixtureClient(_fixture("comic_vine.json"))
    provider = ComicVineProvider(client, "secret")  # type: ignore[arg-type]
    candidate = provider.search(
        SearchQuery(
            MediaKind.COMIC,
            "Absolute Batman",
            series_title="Absolute Batman",
            sequence=SequenceNumber.parse("014"),
        )
    )[0]
    assert candidate.record_type is RecordType.COMIC_ISSUE
    assert candidate.series_title == "Absolute Batman"
    assert candidate.run_start_year == 2024
    assert candidate.sequence == SequenceNumber.parse("14")
    assert candidate.run_id == "4050-160294"
    assert candidate.publisher == "DC Comics"
    assert candidate.title == "Abomination, Conclusion"
    assert candidate.creators == ()
    assert candidate.publication_date is None
    assert candidate.cover_date == "2026-01"
    assert candidate.cover_date_precision == "month"
    assert candidate.release_date == "2025-11-26"
    assert candidate.release_date_precision == "day"
    assert not hasattr(candidate, "volume")
    assert client.calls[0][2]["api_key"] == "secret"


def test_comic_vine_exact_detail_splits_compound_roles_and_preserves_generic_artist() -> None:
    client = FixtureClient(_fixture("comic_vine_issue_detail.json"))
    provider = ComicVineProvider(client, "secret")  # type: ignore[arg-type]

    candidate = provider.fetch("4000-1145497")[0]

    assert candidate.title == "Abomination, Conclusion"
    assert [(item.name, item.role) for item in candidate.creators] == [
        ("Scott Snyder", "writer"),
        ("Nick Dragotta", "artist"),
        ("Frank Martin", "colorist"),
        ("Frank Martin", "cover-artist"),
        ("Clayton Cowles", "letterer"),
    ]
    assert candidate.provider_schema_version == 3


def test_comic_vine_compound_roles_keep_known_and_unknown_tokens_independently() -> None:
    client = FixtureClient(
        {
            "results": {
                "id": 1,
                "resource_type": "issue",
                "name": "Issue",
                "issue_number": "1",
                "volume": {"id": 2, "name": "Series"},
                "person_credits": [
                    {"name": "Person", "role": "writer, story architect, writer"}
                ],
            }
        }
    )
    candidate = ComicVineProvider(client, "secret").fetch("4000-1")[0]  # type: ignore[arg-type]
    assert [(item.name, item.role) for item in candidate.creators] == [
        ("Person", "writer"),
        ("Person", "unknown:story architect"),
    ]


def test_comic_vine_resolves_runs_then_filters_issues_by_run_and_sequence() -> None:
    run_client = FixtureClient(_fixture("comic_vine_runs.json"))
    provider = ComicVineProvider(run_client, "secret")  # type: ignore[arg-type]
    runs = provider.search_runs(
        SearchQuery(MediaKind.COMIC, "Absolute Batman", series_title="Absolute Batman")
    )
    assert [(item.provider_id, item.run_start_year) for item in runs] == [
        ("4050-160294", 2024),
        ("4050-167340", 2025),
    ]
    assert run_client.calls[0][1]["resources"] == "volume"
    assert run_client.calls[0][3] == "search:run"

    issue_client = FixtureClient(_fixture("comic_vine.json"))
    provider = ComicVineProvider(issue_client, "secret")  # type: ignore[arg-type]
    issue = provider.search_issue_in_run(runs[0], SequenceNumber.parse("014"))[0]
    assert issue.run_id == "4050-160294"
    assert issue.run_start_year == 2024
    assert issue_client.calls[0][1]["filter"] == "volume:160294,issue_number:14"
    assert "format" in issue_client.calls[0][1]["field_list"]
    assert issue_client.calls[0][3] == "issues"


@pytest.mark.parametrize(
    ("raw_format", "item_type", "record_type"),
    [
        ("", "issue", RecordType.COMIC_ISSUE),
        ("Annual", "annual", RecordType.COMIC_ISSUE),
        ("One-Shot", "one-shot", RecordType.COMIC_ISSUE),
        ("TPB", "collected-edition", RecordType.COMIC_COLLECTION),
        ("Trade Paperback", "collected-edition", RecordType.COMIC_COLLECTION),
        ("Hardcover", "collected-edition", RecordType.COMIC_COLLECTION),
        ("Omnibus", "omnibus", RecordType.COMIC_COLLECTION),
        ("Graphic Novel", "graphic-novel", RecordType.COMIC_COLLECTION),
    ],
)
def test_comic_vine_normalizes_formats_at_provider_boundary(
    raw_format: str, item_type: str, record_type: RecordType
) -> None:
    payload = _fixture("comic_vine.json")
    assert isinstance(payload, dict)
    payload["results"][0]["format"] = raw_format  # type: ignore[index]
    provider = ComicVineProvider(FixtureClient(payload), "secret")  # type: ignore[arg-type]
    candidate = provider.search(SearchQuery(MediaKind.COMIC, "Absolute Batman"))[0]
    assert candidate.item_type == item_type
    assert candidate.record_type is record_type
    if raw_format:
        assert candidate.provider_metadata["raw_format"] == raw_format
    else:
        assert "raw_format" not in candidate.provider_metadata
    assert candidate.provider_metadata["cover_date_source"] == "cover_date"
    assert candidate.provider_metadata["release_date_source"] == "store_date"


def test_comic_vine_unknown_format_is_preserved_and_not_masqueraded_as_issue() -> None:
    payload = _fixture("comic_vine.json")
    assert isinstance(payload, dict)
    payload["results"][0]["format"] = "Prestige Mystery"  # type: ignore[index]
    provider = ComicVineProvider(FixtureClient(payload), "secret")  # type: ignore[arg-type]
    candidate = provider.search(SearchQuery(MediaKind.COMIC, "Absolute Batman"))[0]
    assert candidate.item_type == "unsupported"
    assert candidate.provider_metadata["raw_format"] == "Prestige Mystery"


@pytest.mark.parametrize(
    ("store_date", "cover_date", "expected_release", "expected_cover", "cover_precision"),
    [
        ("2025-11-26", "2026-01-01", "2025-11-26", "2026-01", "month"),
        (None, "2026-01-01", None, "2026-01", "month"),
        (None, "2026", None, "2026", "year"),
        (None, None, None, None, None),
        ("not-a-date", "2026-99-99", None, None, None),
    ],
)
def test_comic_vine_preserves_date_meaning_and_precision(
    store_date: str | None,
    cover_date: str | None,
    expected_release: str | None,
    expected_cover: str | None,
    cover_precision: str | None,
) -> None:
    payload = _fixture("comic_vine.json")
    assert isinstance(payload, dict)
    issue = payload["results"][0]  # type: ignore[index]
    issue["store_date"] = store_date
    issue["cover_date"] = cover_date
    provider = ComicVineProvider(FixtureClient(payload), "secret")  # type: ignore[arg-type]

    candidate = provider.search(SearchQuery(MediaKind.COMIC, "Absolute Batman"))[0]

    assert candidate.release_date == expected_release
    assert candidate.release_date_precision == ("day" if expected_release else None)
    assert candidate.cover_date == expected_cover
    assert candidate.cover_date_precision == cover_precision
    assert NormalizedCandidate.from_dict(candidate.to_dict()) == candidate


def test_comic_vine_collected_search_uses_collection_oriented_query_bucket() -> None:
    client = FixtureClient(_fixture("comic_vine.json"))
    provider = ComicVineProvider(client, "secret")  # type: ignore[arg-type]
    provider.search(
        SearchQuery(
            MediaKind.COMIC,
            "Animal Man Book 1",
            series_title="Animal Man",
            item_type="collected-edition",
        )
    )
    assert "TPB" in client.calls[0][1]["query"]
    assert client.calls[0][3] == "search:collection"


def test_comic_vine_missing_credential_is_explicit() -> None:
    provider = ComicVineProvider(FixtureClient({"results": []}), None)  # type: ignore[arg-type]
    assert provider.status().enabled is False
    assert provider.status().credential_present is False
    assert "cached" in provider.status().capabilities


def test_provider_response_validation_rejects_malformed_shapes() -> None:
    with pytest.raises(ValueError, match="docs list"):
        OpenLibraryProvider(FixtureClient({"docs": {}}), None).search(  # type: ignore[arg-type]
            SearchQuery(MediaKind.BOOK, "Book")
        )
    with pytest.raises(ValueError, match="items must be a list"):
        GoogleBooksProvider(FixtureClient({"items": {}})).search(  # type: ignore[arg-type]
            SearchQuery(MediaKind.BOOK, "Book")
        )
    with pytest.raises(ValueError, match="requires results"):
        ComicVineProvider(FixtureClient({}), "key").search(  # type: ignore[arg-type]
            SearchQuery(MediaKind.COMIC, "Comic")
        )
