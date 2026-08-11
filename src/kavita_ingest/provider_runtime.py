from __future__ import annotations

import sqlite3

from . import __version__
from .config import ProviderSettings
from .provider_store import ProviderStore
from .providers.base import Provider
from .providers.client import CachedProviderClient
from .providers.comic_vine import ComicVineProvider
from .providers.google_books import GoogleBooksProvider
from .providers.models import ProviderName
from .providers.open_library import OpenLibraryProvider
from .providers.transport import Transport, UrllibTransport
from .rate_limit import DurableRateLimiter, RatePolicy


def build_providers(
    connection: sqlite3.Connection,
    settings: ProviderSettings,
    transport: Transport | None = None,
) -> tuple[Provider, ...]:
    actual_transport = transport or UrllibTransport()
    store = ProviderStore(connection)
    limiter = DurableRateLimiter(connection)
    contact = settings.open_library_contact
    open_library = OpenLibraryProvider(
        CachedProviderClient(
            ProviderName.OPEN_LIBRARY,
            policy=RatePolicy(
                9_000 if contact else 2_880,
                3_600,
                settings.open_library_identified_interval
                if contact
                else settings.open_library_unidentified_interval,
            ),
            user_agent=(
                f"kavita-ingest/{__version__} ({contact})"
                if contact
                else f"kavita-ingest/{__version__}"
            ),
            store=store,
            limiter=limiter,
            transport=actual_transport,
            timeout=settings.timeout_seconds,
            ttl_seconds=settings.cache_ttl_seconds,
            offline=settings.offline,
        ),
        contact,
    )
    google = GoogleBooksProvider(
        CachedProviderClient(
            ProviderName.GOOGLE_BOOKS,
            policy=RatePolicy(10_000, 3_600, settings.google_books_min_interval),
            user_agent=f"kavita-ingest/{__version__}",
            store=store,
            limiter=limiter,
            transport=actual_transport,
            timeout=settings.timeout_seconds,
            ttl_seconds=settings.cache_ttl_seconds,
            offline=settings.offline,
        ),
        settings.google_books_api_key,
    )
    comic_vine = ComicVineProvider(
        CachedProviderClient(
            ProviderName.COMIC_VINE,
            policy=RatePolicy(
                settings.comic_vine_max_requests,
                settings.comic_vine_window_seconds,
                settings.comic_vine_min_interval,
            ),
            user_agent=f"kavita-ingest/{__version__}",
            store=store,
            limiter=limiter,
            transport=actual_transport,
            timeout=settings.timeout_seconds,
            ttl_seconds=settings.cache_ttl_seconds,
            offline=settings.offline,
            network_enabled=bool(settings.comic_vine_api_key),
            unavailable_reason="Comic Vine API key is not configured",
        ),
        settings.comic_vine_api_key,
    )
    providers: list[Provider] = []
    if settings.open_library_enabled:
        providers.append(open_library)
    if settings.google_books_enabled:
        providers.append(google)
    if settings.comic_vine_enabled:
        providers.append(comic_vine)
    return tuple(providers)
