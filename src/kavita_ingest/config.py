from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import AppPaths


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    open_library_contact: str | None = None
    google_books_api_key: str | None = None
    comic_vine_api_key: str | None = None
    offline: bool = False
    cache_ttl_seconds: int = 7 * 24 * 60 * 60
    comic_vine_max_requests: int = 180
    comic_vine_window_seconds: int = 3_600
    comic_vine_min_interval: float = 1.25
    open_library_identified_interval: float = 0.4
    open_library_unidentified_interval: float = 1.25
    google_books_min_interval: float = 0.25
    timeout_seconds: float = 15.0


@dataclass(frozen=True, slots=True)
class MatchingSettings:
    eligible_score: float = 92.0
    eligible_margin: float = 12.0
    classification_confidence: float = 0.90


@dataclass(frozen=True, slots=True)
class AppConfig:
    incoming_roots: tuple[Path, ...] = ()
    books_root: Path | None = None
    comics_root: Path | None = None
    staging_root: Path | None = None
    ignored_roots: tuple[Path, ...] = ()
    database_path: Path | None = None
    log_level: str = "INFO"
    archive_entry_limit: int = 5_000
    archive_entry_size_limit: int = 512 * 1024 * 1024
    archive_total_size_limit: int = 4 * 1024 * 1024 * 1024
    archive_path_depth_limit: int = 20
    archive_ratio_limit: float = 1_000.0
    providers: ProviderSettings = ProviderSettings()
    matching: MatchingSettings = MatchingSettings()

    def excluded_roots(self) -> tuple[Path, ...]:
        values = [*self.ignored_roots]
        values.extend(
            path for path in (self.books_root, self.comics_root, self.staging_root) if path
        )
        return tuple(_resolved(path) for path in values)


def load_config(path: Path | None = None, app_paths: AppPaths | None = None) -> AppConfig:
    locations = app_paths or AppPaths.default()
    config_path = path or locations.config_file
    if not config_path.exists():
        return AppConfig(database_path=locations.database_file)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    paths = _table(raw, "paths")
    archive = _table(raw, "archive")
    logging = _table(raw, "logging")
    providers = _table(raw, "providers")
    open_library = _nested_table(providers, "open_library")
    google_books = _nested_table(providers, "google_books")
    comic_vine = _nested_table(providers, "comic_vine")
    matching = _table(raw, "matching")
    provider_settings = ProviderSettings(
        open_library_contact=_string_or_none(open_library.get("contact"))
        or os.getenv("KAVITA_INGEST_OPEN_LIBRARY_CONTACT"),
        google_books_api_key=_string_or_none(google_books.get("api_key"))
        or os.getenv("GOOGLE_BOOKS_API_KEY"),
        comic_vine_api_key=_string_or_none(comic_vine.get("api_key"))
        or os.getenv("COMIC_VINE_API_KEY"),
        offline=bool(providers.get("offline", False)),
        cache_ttl_seconds=int(providers.get("cache_ttl_seconds", 7 * 24 * 60 * 60)),
        comic_vine_max_requests=int(comic_vine.get("max_requests", 180)),
        comic_vine_window_seconds=int(comic_vine.get("window_seconds", 3_600)),
        comic_vine_min_interval=float(comic_vine.get("min_interval", 1.25)),
        open_library_identified_interval=float(open_library.get("identified_interval", 0.4)),
        open_library_unidentified_interval=float(open_library.get("unidentified_interval", 1.25)),
        google_books_min_interval=float(google_books.get("min_interval", 0.25)),
        timeout_seconds=float(providers.get("timeout_seconds", 15.0)),
    )
    _validate_provider_settings(provider_settings)
    return AppConfig(
        incoming_roots=_path_tuple(paths.get("incoming", [])),
        books_root=_optional_path(paths.get("books")),
        comics_root=_optional_path(paths.get("comics")),
        staging_root=_optional_path(paths.get("staging")),
        ignored_roots=_path_tuple(paths.get("ignore", [])),
        database_path=_optional_path(paths.get("database")) or locations.database_file,
        log_level=str(logging.get("level", "INFO")).upper(),
        archive_entry_limit=int(archive.get("max_entries", 5_000)),
        archive_entry_size_limit=int(archive.get("max_entry_bytes", 512 * 1024 * 1024)),
        archive_total_size_limit=int(archive.get("max_total_bytes", 4 * 1024 * 1024 * 1024)),
        archive_path_depth_limit=int(archive.get("max_path_depth", 20)),
        archive_ratio_limit=float(archive.get("max_ratio", 1_000.0)),
        providers=provider_settings,
        matching=MatchingSettings(
            eligible_score=float(matching.get("eligible_score", 92.0)),
            eligible_margin=float(matching.get("eligible_margin", 12.0)),
            classification_confidence=float(matching.get("classification_confidence", 0.90)),
        ),
    )


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{key}] must be a TOML table")
    return value


def _nested_table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"provider {key!r} must be a TOML table")
    return value


def _path_tuple(values: object) -> tuple[Path, ...]:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError("path lists must contain only strings")
    return tuple(Path(item).expanduser() for item in values)


def _optional_path(value: object) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("path values must be strings")
    return Path(value).expanduser()


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("provider contact and credential values must be strings")
    return value.strip() or None


def _validate_provider_settings(settings: ProviderSettings) -> None:
    if not 1 <= settings.comic_vine_max_requests <= 180:
        raise ValueError("Comic Vine max_requests must be between 1 and 180")
    if settings.comic_vine_window_seconds < 3_600:
        raise ValueError("Comic Vine window_seconds cannot be less than 3600")
    if settings.comic_vine_min_interval < 1.25:
        raise ValueError("Comic Vine min_interval cannot be less than 1.25 seconds")
    if settings.open_library_identified_interval < 0.4:
        raise ValueError("Open Library identified_interval cannot be less than 0.4 seconds")
    if settings.open_library_unidentified_interval < 1.25:
        raise ValueError("Open Library unidentified_interval cannot be less than 1.25 seconds")
    if settings.google_books_min_interval < 0.2:
        raise ValueError("Google Books min_interval cannot be less than 0.2 seconds")
    if settings.cache_ttl_seconds < 60:
        raise ValueError("provider cache TTL must be at least 60 seconds")
