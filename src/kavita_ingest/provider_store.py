from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from .providers.models import NormalizedCandidate, ProviderName


@dataclass(frozen=True, slots=True)
class CacheEntry:
    candidates: tuple[NormalizedCandidate, ...]
    raw: object
    schema_version: int
    fetched_at: float
    expires_at: float
    stale: bool


class ProviderStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get_cache(self, cache_key: str, *, now: float | None = None) -> CacheEntry | None:
        row = self.connection.execute(
            "SELECT normalized_json, raw_json, schema_version, fetched_at, expires_at "
            "FROM provider_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        current = time.time() if now is None else now
        normalized = json.loads(row[0])
        return CacheEntry(
            tuple(NormalizedCandidate.from_dict(item) for item in normalized),
            json.loads(row[1]),
            int(row[2]),
            float(row[3]),
            float(row[4]),
            current >= float(row[4]),
        )

    def rewrite_cache_normalization(
        self,
        cache_key: str,
        candidates: list[NormalizedCandidate],
        schema_version: int,
    ) -> None:
        normalized_json = _normalized_json(candidates, schema_version)
        cursor = self.connection.execute(
            "UPDATE provider_cache SET normalized_json = ?, schema_version = ? "
            "WHERE cache_key = ?",
            (
                normalized_json,
                schema_version,
                cache_key,
            ),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise KeyError(f"provider cache entry disappeared during schema migration: {cache_key}")
        self.connection.commit()

    def put_cache(
        self,
        cache_key: str,
        provider: ProviderName,
        operation: str,
        request: dict[str, object],
        candidates: list[NormalizedCandidate],
        raw: object,
        schema_version: int,
        ttl_seconds: float,
        *,
        now: float | None = None,
    ) -> None:
        current = time.time() if now is None else now
        normalized_json = _normalized_json(candidates, schema_version)
        self.connection.execute(
            """
            INSERT INTO provider_cache(cache_key, provider, operation, request_json,
              normalized_json, raw_json, schema_version, fetched_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              normalized_json=excluded.normalized_json, raw_json=excluded.raw_json,
              schema_version=excluded.schema_version, fetched_at=excluded.fetched_at,
              expires_at=excluded.expires_at
            """,
            (
                cache_key,
                provider.value,
                operation,
                json.dumps(request, sort_keys=True),
                normalized_json,
                json.dumps(raw, sort_keys=True),
                schema_version,
                current,
                current + ttl_seconds,
            ),
        )
        self.connection.commit()

    def cache_counts(self, *, now: float | None = None) -> tuple[int, int]:
        current = time.time() if now is None else now
        row = self.connection.execute(
            "SELECT count(*), sum(CASE WHEN expires_at <= ? THEN 1 ELSE 0 END) FROM provider_cache",
            (current,),
        ).fetchone()
        return int(row[0]), int(row[1] or 0)


def _normalized_json(candidates: list[NormalizedCandidate], schema_version: int) -> str:
    incompatible = [
        item.provider_schema_version
        for item in candidates
        if item.provider_schema_version != schema_version
    ]
    if incompatible:
        raise ValueError(
            f"candidate schema {incompatible[0]} does not match cache schema {schema_version}"
        )
    return json.dumps([item.to_dict() for item in candidates], sort_keys=True, default=str)
