from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

from ..provider_store import ProviderStore
from ..rate_limit import DurableRateLimiter, RatePolicy
from .base import MalformedProviderResponse, ProviderError, ProviderUnavailable
from .models import NormalizedCandidate, ProviderName, canonical_request_key
from .transport import Transport

Normalizer = Callable[[object], list[NormalizedCandidate]]


@dataclass(slots=True)
class ClientActivity:
    cache_hits: int = 0
    cache_misses: int = 0
    cache_schema_migrations: int = 0
    exact_detail_hydrations: int = 0
    network_requests: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    rate_limit_events: int = 0

    def snapshot(self) -> dict[str, object]:
        return {
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_schema_migrations": self.cache_schema_migrations,
            "exact_detail_hydrations": self.exact_detail_hydrations,
            "network_requests": dict(sorted(self.network_requests.items())),
            "errors": self.errors,
            "rate_limit_events": self.rate_limit_events,
        }


class CachedProviderClient:
    def __init__(
        self,
        provider: ProviderName,
        store: ProviderStore,
        limiter: DurableRateLimiter,
        transport: Transport,
        policy: RatePolicy,
        *,
        user_agent: str,
        timeout: float,
        ttl_seconds: float,
        normalization_schema_version: int,
        offline: bool = False,
        network_enabled: bool = True,
        unavailable_reason: str | None = None,
        max_retries: int = 1,
    ) -> None:
        self.provider = provider
        self.store = store
        self.limiter = limiter
        self.transport = transport
        self.policy = policy
        self.user_agent = user_agent
        self.timeout = timeout
        self.ttl_seconds = ttl_seconds
        self.normalization_schema_version = normalization_schema_version
        self.offline = offline
        self.network_enabled = network_enabled
        self.unavailable_reason = unavailable_reason
        self.max_retries = max_retries
        self.activity = ClientActivity()

    def get(
        self,
        operation: str,
        url: str,
        public_params: dict[str, str],
        secret_params: dict[str, str],
        bucket: str,
        normalize: Normalizer,
    ) -> list[NormalizedCandidate]:
        if operation == "fetch":
            self.activity.exact_detail_hydrations += 1
        request_identity: dict[str, object] = {"url": url, "params": public_params}
        cache_key = canonical_request_key(self.provider, operation, request_identity)
        cached = self.store.get_cache(cache_key)
        cached_schema_matches = cached is not None and all(
            candidate.provider_schema_version == self.normalization_schema_version
            for candidate in cached.candidates
        )
        if cached is not None and (
            cached.schema_version != self.normalization_schema_version
            or not cached_schema_matches
        ):
            if not cached.stale or self.offline:
                try:
                    candidates = self._normalize(normalize, cached.raw, source="cached response")
                    self.store.rewrite_cache_normalization(
                        cache_key, candidates, self.normalization_schema_version
                    )
                except (MalformedProviderResponse, KeyError) as exc:
                    if self.offline:
                        raise ProviderUnavailable(
                            f"{self.provider.value} cached response is incompatible with "
                            f"normalization schema {self.normalization_schema_version}: {exc}"
                        ) from exc
                else:
                    self.activity.cache_hits += 1
                    self.activity.cache_schema_migrations += 1
                    return candidates
        elif cached is not None and not cached.stale:
            self.activity.cache_hits += 1
            return list(cached.candidates)
        if self.offline:
            if cached is not None and cached.schema_version == self.normalization_schema_version:
                self.activity.cache_hits += 1
                return list(cached.candidates)
            raise ProviderUnavailable(f"{self.provider.value} has no cached result for offline use")
        if not self.network_enabled:
            raise ProviderUnavailable(
                self.unavailable_reason or f"{self.provider.value} network access is disabled"
            )

        self.activity.cache_misses += 1
        params = {**public_params, **secret_params}
        last_error: ProviderError | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.wait_and_reserve(self.provider.value, bucket, self.policy)
            self.activity.network_requests[bucket] = (
                self.activity.network_requests.get(bucket, 0) + 1
            )
            try:
                response = self.transport.get(
                    url,
                    params,
                    {"User-Agent": self.user_agent, "Accept": "application/json"},
                    self.timeout,
                )
            except ProviderError as exc:
                self.activity.errors += 1
                last_error = exc
                if attempt < self.max_retries:
                    continue
                raise
            if response.status == 429:
                self.activity.errors += 1
                self.activity.rate_limit_events += 1
                delay = _retry_after(response.headers.get("retry-after"))
                self.limiter.block(self.provider.value, bucket, delay, "HTTP 429")
                raise ProviderError(f"{self.provider.value} rate limited the request")
            if response.status >= 500 and attempt < self.max_retries:
                self.activity.errors += 1
                last_error = ProviderError(f"provider HTTP {response.status}")
                continue
            if response.status < 200 or response.status >= 300:
                self.activity.errors += 1
                raise ProviderError(f"provider HTTP {response.status}")
            raw = response.json()
            candidates = self._normalize(normalize, raw, source="response")
            self.store.put_cache(
                cache_key,
                self.provider,
                operation,
                request_identity,
                candidates,
                raw,
                self.normalization_schema_version,
                self.ttl_seconds,
            )
            return candidates
        raise last_error or ProviderError("provider request failed")

    def _normalize(
        self, normalize: Normalizer, raw: object, *, source: str
    ) -> list[NormalizedCandidate]:
        try:
            candidates = normalize(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedProviderResponse(
                f"{self.provider.value} {source} failed validation: {exc}"
            ) from exc
        incompatible = [
            candidate.provider_schema_version
            for candidate in candidates
            if candidate.provider_schema_version != self.normalization_schema_version
        ]
        if incompatible:
            raise MalformedProviderResponse(
                f"{self.provider.value} {source} produced candidate schema "
                f"{incompatible[0]}, expected {self.normalization_schema_version}"
            )
        return candidates


def _retry_after(value: str | None) -> float:
    if not value:
        return 60.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return 60.0
