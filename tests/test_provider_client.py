from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kavita_ingest.db import connect, migrate
from kavita_ingest.domain import MediaKind
from kavita_ingest.provider_store import ProviderStore
from kavita_ingest.providers.base import (
    MalformedProviderResponse,
    ProviderError,
    ProviderUnavailable,
)
from kavita_ingest.providers.client import CachedProviderClient
from kavita_ingest.providers.models import (
    NormalizedCandidate,
    ProviderName,
    RecordType,
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
) -> tuple[CachedProviderClient, sqlite3.Connection]:
    path = tmp_path / "state.sqlite3"
    migrate(path)
    connection = connect(path)
    return (
        CachedProviderClient(
            ProviderName.GOOGLE_BOOKS,
            ProviderStore(connection),
            DurableRateLimiter(connection, sleeper=lambda _: None),
            transport,
            RatePolicy(100, 3_600, 0),
            user_agent="kavita-ingest/0.1 (contact@example.test)",
            timeout=1,
            ttl_seconds=3_600,
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
