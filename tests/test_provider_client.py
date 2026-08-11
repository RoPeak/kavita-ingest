from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from kavita_ingest.db import connect, migrate
from kavita_ingest.domain import MediaKind, SequenceNumber
from kavita_ingest.provider_store import ProviderStore
from kavita_ingest.providers.base import (
    MalformedProviderResponse,
    ProviderError,
    ProviderUnavailable,
)
from kavita_ingest.providers.client import CachedProviderClient
from kavita_ingest.providers.comic_vine import ComicVineProvider
from kavita_ingest.providers.models import (
    Contributor,
    NormalizedCandidate,
    ProviderName,
    RecordType,
    SearchQuery,
    canonical_request_key,
)
from kavita_ingest.providers.transport import HttpResponse
from kavita_ingest.rate_limit import DurableRateLimiter, RatePolicy


class FakeTransport:
    def __init__(self, responses: list[HttpResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[dict[str, str], dict[str, str]]] = []

    def get(
        self, url: str, params: dict[str, str], headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        self.calls.append((params, headers))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(
    tmp_path: Path,
    transport: FakeTransport,
    *,
    offline: bool = False,
    retries: int = 1,
    network_enabled: bool = True,
    provider: ProviderName = ProviderName.GOOGLE_BOOKS,
    normalization_schema_version: int = 1,
) -> tuple[CachedProviderClient, sqlite3.Connection]:
    path = tmp_path / "state.sqlite3"
    migrate(path)
    connection = connect(path)
    return (
        CachedProviderClient(
            provider,
            ProviderStore(connection),
            DurableRateLimiter(connection, sleeper=lambda _: None),
            transport,
            RatePolicy(100, 3_600, 0),
            user_agent="kavita-ingest/0.1 (contact@example.test)",
            timeout=1,
            ttl_seconds=3_600,
            normalization_schema_version=normalization_schema_version,
            offline=offline,
            network_enabled=network_enabled,
            unavailable_reason="credential missing",
            max_retries=retries,
        ),
        connection,
    )


def _normalize(raw: object) -> list[NormalizedCandidate]:
    if not isinstance(raw, dict) or "title" not in raw:
        raise ValueError("title missing")
    return [
        NormalizedCandidate(
            ProviderName.GOOGLE_BOOKS,
            "id",
            RecordType.BOOK_EDITION,
            MediaKind.BOOK,
            str(raw["title"]),
        )
    ]


def test_cache_avoids_duplicate_traffic_and_sends_user_agent(tmp_path: Path) -> None:
    transport = FakeTransport([HttpResponse(200, {}, b'{"title":"Fixture"}')])
    client, connection = _client(tmp_path, transport)
    first = client.get(
        "search", "https://example.test", {"q": "x"}, {"key": "secret"}, "books", _normalize
    )
    second = client.get(
        "search", "https://example.test", {"q": "x"}, {"key": "different"}, "books", _normalize
    )
    assert first == second
    assert len(transport.calls) == 1
    assert transport.calls[0][0]["key"] == "secret"
    assert "contact@example.test" in transport.calls[0][1]["User-Agent"]
    assert client.activity.snapshot() == {
        "cache_hits": 1,
        "cache_misses": 1,
        "cache_schema_migrations": 0,
        "exact_detail_hydrations": 0,
        "network_requests": {"books": 1},
        "errors": 0,
        "rate_limit_events": 0,
    }
    connection.close()


def test_offline_mode_uses_stale_cache_and_fails_on_miss(tmp_path: Path) -> None:
    transport = FakeTransport([])
    client, connection = _client(tmp_path, transport, offline=True)
    store = client.store
    request = {"url": "https://example.test", "params": {"q": "cached"}}
    cache_key = canonical_request_key(ProviderName.GOOGLE_BOOKS, "search", request)
    store.put_cache(
        cache_key,
        ProviderName.GOOGLE_BOOKS,
        "search",
        request,
        [_normalize({"title": "Old"})[0]],
        {},
        1,
        10,
        now=1,
    )
    cached = client.get("search", "https://example.test", {"q": "cached"}, {}, "books", _normalize)
    assert cached[0].title == "Old"
    with pytest.raises(ProviderUnavailable, match="no cached result"):
        client.get("search", "https://example.test", {"q": "miss"}, {}, "books", _normalize)
    connection.close()


def test_retries_reserve_each_attempt_and_network_errors_surface(tmp_path: Path) -> None:
    transport = FakeTransport(
        [HttpResponse(500, {}, b"{}"), HttpResponse(200, {}, b'{"title":"Recovered"}')]
    )
    client, connection = _client(tmp_path, transport)
    assert (
        client.get("search", "https://example.test", {}, {}, "books", _normalize)[0].title
        == "Recovered"
    )
    count = connection.execute("SELECT count(*) FROM provider_rate_reservations").fetchone()[0]
    assert count == 2
    connection.close()

    transport = FakeTransport([ProviderError("timeout"), ProviderError("timeout")])
    client, connection = _client(tmp_path / "second", transport)
    with pytest.raises(ProviderError, match="timeout"):
        client.get("search", "https://example.test", {}, {}, "books", _normalize)
    assert connection.execute("SELECT count(*) FROM provider_rate_reservations").fetchone()[0] == 2
    connection.close()


def test_malformed_response_is_rejected(tmp_path: Path) -> None:
    transport = FakeTransport([HttpResponse(200, {}, b'{"wrong":true}')])
    client, connection = _client(tmp_path, transport)
    with pytest.raises(MalformedProviderResponse, match="failed validation"):
        client.get("search", "https://example.test", {}, {}, "books", _normalize)
    connection.close()


def test_http_429_persists_block_without_blind_retry(tmp_path: Path) -> None:
    transport = FakeTransport([HttpResponse(429, {"retry-after": "120"}, b"{}")])
    client, connection = _client(tmp_path, transport)
    with pytest.raises(ProviderError, match="rate limited"):
        client.get("search", "https://example.test", {}, {}, "books", _normalize)
    assert len(transport.calls) == 1
    assert connection.execute("SELECT count(*) FROM provider_rate_reservations").fetchone()[0] == 1
    block = connection.execute("SELECT reason FROM provider_blocks").fetchone()[0]
    assert block == "HTTP 429"
    connection.close()


def test_missing_credential_still_allows_cache_but_blocks_live_miss(tmp_path: Path) -> None:
    client, connection = _client(tmp_path, FakeTransport([]), network_enabled=False, retries=0)
    cached_request = {"url": "https://example.test", "params": {"q": "cached"}}
    key = canonical_request_key(ProviderName.GOOGLE_BOOKS, "search", cached_request)
    client.store.put_cache(
        key,
        ProviderName.GOOGLE_BOOKS,
        "search",
        cached_request,
        [_normalize({"title": "Cached"})[0]],
        {},
        1,
        3_600,
    )
    assert (
        client.get("search", "https://example.test", {"q": "cached"}, {}, "books", _normalize)[
            0
        ].title
        == "Cached"
    )
    with pytest.raises(ProviderUnavailable, match="credential missing"):
        client.get("search", "https://example.test", {"q": "miss"}, {}, "books", _normalize)
    connection.close()


def _legacy_comic_vine_request() -> tuple[str, dict[str, str]]:
    return (
        "https://comicvine.gamespot.com/api/search/",
        {
            "query": "Absolute Batman 14",
            "resources": "issue,volume",
            "limit": "10",
            "field_list": (
                "id,resource_type,api_detail_url,name,issue_number,cover_date,"
                "store_date,volume,person_credits,format"
            ),
        },
    )


def _seed_legacy_comic_vine_cache(
    client: CachedProviderClient, *, now: float, ttl_seconds: float
) -> tuple[str, str, float, float]:
    url, params = _legacy_comic_vine_request()
    request = {"url": url, "params": params}
    cache_key = canonical_request_key(ProviderName.COMIC_VINE, "search", request)
    raw = json.loads(
        (Path(__file__).parent / "fixtures/providers/comic_vine.json").read_text(encoding="utf-8")
    )
    obsolete = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4000-1145497",
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        "Abomination, Conclusion",
        publication_date="2026-01-01",
        series_title="Absolute Batman",
        provider_schema_version=1,
    )
    client.store.put_cache(
        cache_key,
        ProviderName.COMIC_VINE,
        "search",
        request,
        [obsolete],
        raw,
        1,
        ttl_seconds,
        now=now,
    )
    row = client.store.connection.execute(
        "SELECT raw_json, fetched_at, expires_at FROM provider_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    return cache_key, str(row[0]), float(row[1]), float(row[2])


@pytest.mark.parametrize(("offline", "stale"), [(False, False), (True, True)])
def test_comic_vine_cache_schema_upgrade_renormalizes_raw_without_network(
    tmp_path: Path, offline: bool, stale: bool
) -> None:
    transport = FakeTransport([])
    client, connection = _client(
        tmp_path,
        transport,
        offline=offline,
        provider=ProviderName.COMIC_VINE,
        normalization_schema_version=ComicVineProvider.normalization_schema_version,
    )
    now = 1.0 if stale else time.time()
    cache_key, raw_json, fetched_at, expires_at = _seed_legacy_comic_vine_cache(
        client, now=now, ttl_seconds=10.0 if stale else 3_600.0
    )
    provider = ComicVineProvider(client, "unused-cache-only-key")

    candidate = provider.search(
        SearchQuery(
            MediaKind.COMIC,
            "Absolute Batman",
            series_title="Absolute Batman",
            sequence=SequenceNumber.parse("14"),
        )
    )[0]

    assert transport.calls == []
    assert candidate.publication_date is None
    assert candidate.cover_date == "2026-01"
    assert candidate.cover_date_precision == "month"
    assert candidate.release_date == "2025-11-26"
    assert candidate.release_date_precision == "day"
    assert candidate.provider_schema_version == 3
    migrated = client.store.get_cache(cache_key, now=now + 1)
    assert migrated is not None and migrated.schema_version == 3
    row = connection.execute(
        "SELECT raw_json, fetched_at, expires_at FROM provider_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    assert (str(row[0]), float(row[1]), float(row[2])) == (raw_json, fetched_at, expires_at)
    assert client.activity.cache_schema_migrations == 1
    connection.close()


def test_fresh_same_schema_cache_does_not_renormalize(tmp_path: Path) -> None:
    client, connection = _client(tmp_path, FakeTransport([]))
    request = {"url": "https://example.test", "params": {"q": "cached"}}
    cache_key = canonical_request_key(ProviderName.GOOGLE_BOOKS, "search", request)
    client.store.put_cache(
        cache_key,
        ProviderName.GOOGLE_BOOKS,
        "search",
        request,
        [_normalize({"title": "Cached"})[0]],
        {"wrong": True},
        1,
        3_600,
    )

    def forbidden(_: object) -> list[NormalizedCandidate]:
        raise AssertionError("same-schema cache hit must not normalize raw data")

    candidates = client.get(
        "search", "https://example.test", {"q": "cached"}, {}, "books", forbidden
    )
    assert candidates[0].title == "Cached"
    assert client.activity.cache_schema_migrations == 0
    connection.close()


def test_stale_schema_mismatch_refreshes_from_network_when_online(tmp_path: Path) -> None:
    transport = FakeTransport([HttpResponse(200, {}, b'{"title":"Fresh"}')])
    client, connection = _client(tmp_path, transport, normalization_schema_version=1)
    request = {"url": "https://example.test", "params": {"q": "cached"}}
    cache_key = canonical_request_key(ProviderName.GOOGLE_BOOKS, "search", request)
    client.store.put_cache(
        cache_key,
        ProviderName.GOOGLE_BOOKS,
        "search",
        request,
        [],
        {"title": "Stale"},
        0,
        1,
        now=1,
    )
    result = client.get(
        "search", "https://example.test", {"q": "cached"}, {}, "books", _normalize
    )
    assert result[0].title == "Fresh"
    assert len(transport.calls) == 1
    assert client.activity.cache_schema_migrations == 0
    connection.close()


@pytest.mark.parametrize("offline", [False, True])
def test_failed_cache_schema_migration_never_uses_obsolete_normalized_data(
    tmp_path: Path, offline: bool
) -> None:
    responses = [] if offline else [HttpResponse(200, {}, b'{"title":"Network"}')]
    client, connection = _client(
        tmp_path, FakeTransport(responses), offline=offline, normalization_schema_version=2
    )
    request = {"url": "https://example.test", "params": {"q": "cached"}}
    cache_key = canonical_request_key(ProviderName.GOOGLE_BOOKS, "search", request)
    client.store.put_cache(
        cache_key,
        ProviderName.GOOGLE_BOOKS,
        "search",
        request,
        [_normalize({"title": "Obsolete"})[0]],
        {"wrong": True},
        1,
        3_600,
    )

    def schema_two_normalizer(raw: object) -> list[NormalizedCandidate]:
        if not isinstance(raw, dict) or "title" not in raw:
            raise ValueError("title missing")
        return [
            NormalizedCandidate(
                ProviderName.GOOGLE_BOOKS,
                "current",
                RecordType.BOOK_EDITION,
                MediaKind.BOOK,
                str(raw["title"]),
                provider_schema_version=2,
            )
        ]

    if offline:
        with pytest.raises(ProviderUnavailable, match="incompatible with normalization schema 2"):
            client.get(
                "search",
                "https://example.test",
                {"q": "cached"},
                {},
                "books",
                schema_two_normalizer,
            )
    else:
        result = client.get(
            "search",
            "https://example.test",
            {"q": "cached"},
            {},
            "books",
            schema_two_normalizer,
        )
        assert result[0].title == "Network"
    connection.close()


def test_offline_exact_detail_cache_migrates_compound_roles_without_network(
    tmp_path: Path,
) -> None:
    client, connection = _client(
        tmp_path,
        FakeTransport([]),
        offline=True,
        provider=ProviderName.COMIC_VINE,
        normalization_schema_version=ComicVineProvider.normalization_schema_version,
    )
    url = "https://comicvine.gamespot.com/api/issue/4000-1145497/"
    request = {"url": url, "params": {}}
    cache_key = canonical_request_key(ProviderName.COMIC_VINE, "fetch", request)
    raw = json.loads(
        (
            Path(__file__).parent / "fixtures/providers/comic_vine_issue_detail.json"
        ).read_text(encoding="utf-8")
    )
    obsolete = NormalizedCandidate(
        ProviderName.COMIC_VINE,
        "4000-1145497",
        RecordType.COMIC_ISSUE,
        MediaKind.COMIC,
        "Abomination, Conclusion",
        creators=(Contributor("Frank Martin", "unknown:colorist cover"),),
        provider_schema_version=2,
    )
    client.store.put_cache(
        cache_key,
        ProviderName.COMIC_VINE,
        "fetch",
        request,
        [obsolete],
        raw,
        2,
        3_600,
    )

    candidate = ComicVineProvider(client, "unused-cache-only-key").fetch("4000-1145497")[0]

    assert ("Frank Martin", "colorist") in [
        (item.name, item.role) for item in candidate.creators
    ]
    assert ("Frank Martin", "cover-artist") in [
        (item.name, item.role) for item in candidate.creators
    ]
    assert client.activity.exact_detail_hydrations == 1
    assert client.activity.cache_schema_migrations == 1
    assert client.store.get_cache(cache_key).schema_version == 3  # type: ignore[union-attr]
    connection.close()
