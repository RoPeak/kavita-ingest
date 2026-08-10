from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kavita_ingest.db import connect, migrate
from kavita_ingest.domain import MediaKind
from kavita_ingest.provider_store import ProviderStore
from kavita_ingest.providers.models import NormalizedCandidate, ProviderName, RecordType
from kavita_ingest.rate_limit import DurableRateLimiter, RatePolicy


def _database(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    path = tmp_path / "state.sqlite3"
    migrate(path)
    return path, connect(path)


def _candidate() -> NormalizedCandidate:
    return NormalizedCandidate(
        ProviderName.GOOGLE_BOOKS,
        "volume-1",
        RecordType.BOOK_EDITION,
        MediaKind.BOOK,
        "Fixture Book",
    )


def test_provider_cache_hit_miss_expiry_and_raw_provenance(tmp_path: Path) -> None:
    _, connection = _database(tmp_path)
    store = ProviderStore(connection)
    assert store.get_cache("missing", now=100) is None
    store.put_cache(
        "key",
        ProviderName.GOOGLE_BOOKS,
        "search",
        {"q": "fixture"},
        [_candidate()],
        {"raw": True},
        1,
        10,
        now=100,
    )
    fresh = store.get_cache("key", now=109)
    stale = store.get_cache("key", now=110)
    assert fresh is not None and fresh.stale is False
    assert fresh.candidates[0].provider_id == "volume-1"
    assert fresh.raw == {"raw": True}
    assert stale is not None and stale.stale is True
    assert store.cache_counts(now=110) == (1, 1)
    connection.close()


class Clock:
    def __init__(self, value: float = 1_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def test_durable_rolling_window_and_minimum_interval(tmp_path: Path) -> None:
    path, connection = _database(tmp_path)
    clock = Clock()
    policy = RatePolicy(2, 100, 10)
    limiter = DurableRateLimiter(connection, clock=clock, sleeper=clock.sleep)
    assert limiter.reserve("comic_vine", "issues", policy).allowed
    blocked_interval = limiter.reserve("comic_vine", "issues", policy)
    assert blocked_interval.allowed is False
    assert blocked_interval.wait_seconds == pytest.approx(10)
    clock.sleep(10)
    assert limiter.reserve("comic_vine", "issues", policy).allowed
    quota = limiter.reserve("comic_vine", "issues", policy)
    assert quota.allowed is False
    assert quota.wait_seconds == pytest.approx(90)
    connection.close()

    restarted = connect(path)
    limiter_after_restart = DurableRateLimiter(restarted, clock=clock, sleeper=clock.sleep)
    assert limiter_after_restart.reserve("comic_vine", "issues", policy).allowed is False
    clock.sleep(90)
    assert limiter_after_restart.reserve("comic_vine", "issues", policy).allowed
    restarted.close()


def test_provider_block_can_only_lengthen_wait(tmp_path: Path) -> None:
    _, connection = _database(tmp_path)
    clock = Clock()
    limiter = DurableRateLimiter(connection, clock=clock, sleeper=clock.sleep)
    policy = RatePolicy(180, 3_600, 1.25)
    limiter.block("comic_vine", "search", 60, "Retry-After")
    blocked = limiter.reserve("comic_vine", "search", policy)
    assert blocked.wait_seconds == pytest.approx(60)
    limiter.block("comic_vine", "search", 5, "shorter response")
    assert limiter.reserve("comic_vine", "search", policy).wait_seconds == pytest.approx(60)
    connection.close()
