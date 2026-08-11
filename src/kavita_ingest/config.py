from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive_safety import ArchiveLimits
from .naming import NamingPolicy
from .paths import AppPaths


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    open_library_enabled: bool = True
    google_books_enabled: bool = True
    comic_vine_enabled: bool = True
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
    source_lifecycle: str = "move_after_verify"
    source_archive_root: Path | None = None
    cbr_conversion_enabled: bool = True
    book_folder_template: str = "{series_or_title}"
    book_filename_template: str = "{title}"
    book_series_filename_template: str = "{series} - {number} - {title}"
    comic_folder_template: str = "{series}"
    comic_filename_template: str = "{series} - {number} - {title}"
    integer_sequence_padding: int = 3
    comic_specials_subfolder: bool = True
    published_file_mode: int = 0o644
    created_directory_mode: int = 0o755
    providers: ProviderSettings = ProviderSettings()
    matching: MatchingSettings = MatchingSettings()

    def excluded_roots(self) -> tuple[Path, ...]:
        values = [*self.ignored_roots]
        values.extend(
            path for path in (self.books_root, self.comics_root, self.staging_root) if path
        )
        return tuple(_resolved(path) for path in values)

    def archive_limits(self) -> ArchiveLimits:
        return ArchiveLimits(
            self.archive_entry_limit,
            self.archive_entry_size_limit,
            self.archive_total_size_limit,
            self.archive_path_depth_limit,
            self.archive_ratio_limit,
        )

    def naming_policy(self) -> NamingPolicy:
        return NamingPolicy(
            book_folder=self.book_folder_template,
            book_file=self.book_filename_template,
            book_series_file=self.book_series_filename_template,
            comic_folder=self.comic_folder_template,
            comic_file=self.comic_filename_template,
            integer_padding=self.integer_sequence_padding,
            comic_specials_subfolder=self.comic_specials_subfolder,
        )


def load_config(path: Path | None = None, app_paths: AppPaths | None = None) -> AppConfig:
    locations = app_paths or AppPaths.default()
    config_path = path or locations.config_file
    if config_path.exists():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    else:
        raw = {}
    paths = _table(raw, "paths")
    archive = _table(raw, "archive")
    source = _table(raw, "source")
    cbr = _table(raw, "cbr")
    naming = _table(raw, "naming")
    sequence = _table(raw, "sequence")
    permissions = _table(raw, "permissions")
    logging = _table(raw, "logging")
    providers = _table(raw, "providers")
    open_library = _nested_table(providers, "open_library")
    google_books = _nested_table(providers, "google_books")
    comic_vine = _nested_table(providers, "comic_vine")
    matching = _table(raw, "matching")
    provider_settings = ProviderSettings(
        open_library_enabled=_boolean(open_library.get("enabled", True), "open_library.enabled"),
        google_books_enabled=_boolean(google_books.get("enabled", True), "google_books.enabled"),
        comic_vine_enabled=_boolean(comic_vine.get("enabled", True), "comic_vine.enabled"),
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
    if comic_vine.get("enabled") is True and not provider_settings.comic_vine_api_key:
        raise ValueError(
            "Comic Vine is explicitly enabled but COMIC_VINE_API_KEY is not configured"
        )
    config = AppConfig(
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
        source_lifecycle=str(source.get("lifecycle", "move_after_verify")),
        source_archive_root=_optional_path(source.get("archive_root")),
        cbr_conversion_enabled=_boolean(cbr.get("convert_to_cbz", True), "cbr.convert_to_cbz"),
        book_folder_template=str(naming.get("book_folder", "{series_or_title}")),
        book_filename_template=str(naming.get("book", "{title}")),
        book_series_filename_template=str(
            naming.get("book_series", "{series} - {number} - {title}")
        ),
        comic_folder_template=str(naming.get("comic_folder", "{series}")),
        comic_filename_template=str(
            naming.get("comic", "{series} - {number} - {title}")
        ),
        integer_sequence_padding=int(sequence.get("integer_padding", 3)),
        comic_specials_subfolder=_boolean(
            naming.get("comic_specials_subfolder", True),
            "naming.comic_specials_subfolder",
        ),
        published_file_mode=_permission_mode(
            permissions.get("file_mode", "0644"), "permissions.file_mode", directory=False
        ),
        created_directory_mode=_permission_mode(
            permissions.get("directory_mode", "0755"),
            "permissions.directory_mode",
            directory=True,
        ),
        providers=provider_settings,
        matching=MatchingSettings(
            eligible_score=float(matching.get("eligible_score", 92.0)),
            eligible_margin=float(matching.get("eligible_margin", 12.0)),
            classification_confidence=float(matching.get("classification_confidence", 0.90)),
        ),
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    errors: list[str] = []
    if (
        config.books_root
        and config.comics_root
        and _resolved(config.books_root) == _resolved(config.comics_root)
    ):
        errors.append("Books and Comics destination roots must be different")
    destinations = tuple(
        path for path in (config.books_root, config.comics_root) if path is not None
    )
    for incoming in config.incoming_roots:
        for destination in destinations:
            incoming_resolved = _resolved(incoming)
            destination_resolved = _resolved(destination)
            if _contains(incoming_resolved, destination_resolved):
                errors.append(
                    f"destination {destination} is nested inside incoming root {incoming}"
                )
            elif _contains(destination_resolved, incoming_resolved):
                errors.append(
                    f"incoming root {incoming} is nested inside destination {destination}"
                )
    if config.staging_root and config.staging_root.exists():
        for destination in destinations:
            if (
                destination.exists()
                and config.staging_root.stat().st_dev != destination.stat().st_dev
            ):
                errors.append(
                    f"staging root {config.staging_root} is on a different filesystem from "
                    f"destination {destination}; apply stages beside each destination instead"
                )
    if config.source_lifecycle not in {
        "move_after_verify",
        "preserve",
        "archive_after_verify",
    }:
        errors.append(
            "source lifecycle must be move_after_verify, preserve, or archive_after_verify"
        )
    if config.source_lifecycle == "archive_after_verify" and config.source_archive_root is None:
        errors.append("archive_after_verify requires source.archive_root")
    if config.source_archive_root is not None:
        archive = _resolved(config.source_archive_root)
        for other in (*config.incoming_roots, *destinations):
            resolved = _resolved(other)
            if archive == resolved or _contains(archive, resolved) or _contains(resolved, archive):
                errors.append(
                    f"source archive root {config.source_archive_root} must be separate "
                    f"from {other}"
                )
    if min(
        config.archive_entry_limit,
        config.archive_entry_size_limit,
        config.archive_total_size_limit,
        config.archive_path_depth_limit,
    ) <= 0:
        errors.append("archive limits must be positive")
    if config.archive_entry_size_limit > config.archive_total_size_limit:
        errors.append("archive max_entry_bytes cannot exceed max_total_bytes")
    if config.archive_ratio_limit <= 1:
        errors.append("archive max_ratio must be greater than 1")
    if not 0 <= config.matching.eligible_score <= 100:
        errors.append("matching eligible_score must be between 0 and 100")
    if not 0 <= config.matching.eligible_margin <= 100:
        errors.append("matching eligible_margin must be between 0 and 100")
    if not 0 <= config.matching.classification_confidence <= 1:
        errors.append("matching classification_confidence must be between 0 and 1")
    if not 1 <= config.integer_sequence_padding <= 12:
        errors.append("sequence integer_padding must be between 1 and 12")
    try:
        config.naming_policy().validate()
    except ValueError as exc:
        errors.append(str(exc))
    if errors:
        raise ValueError("invalid configuration:\n- " + "\n- ".join(errors))


def write_initial_config(path: Path, *, force: bool = False) -> Path:
    if path.exists() and not force:
        raise FileExistsError(f"configuration already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(INITIAL_CONFIG, encoding="utf-8")
    return path


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


def _contains(parent: Path, child: Path) -> bool:
    return parent != child and child.is_relative_to(parent)


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("provider contact and credential values must be strings")
    return value.strip() or None


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _permission_mode(value: object, name: str, *, directory: bool) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0[0-7]{3}", value):
        raise ValueError(f"{name} must be a quoted four-digit octal string such as '0644'")
    mode = int(value, 8)
    if mode & 0o002:
        raise ValueError(f"{name} must not be world-writable")
    if directory:
        if mode & 0o700 != 0o700:
            raise ValueError(f"{name} must grant the owner read, write and search permissions")
    elif mode & 0o111:
        raise ValueError(f"{name} must not make published media executable")
    return mode


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


INITIAL_CONFIG = '''# Kavita Ingest configuration
# Provider secrets belong in environment variables, never this file:
#   COMIC_VINE_API_KEY
#   GOOGLE_BOOKS_API_KEY
# Open Library needs no API key, but identified contact is strongly recommended:
#   KAVITA_INGEST_OPEN_LIBRARY_CONTACT

[paths]
incoming = ["~/Incoming/Reading"]
books = "~/Libraries/Kavita/Books"
comics = "~/Libraries/Kavita/Comics"
# Apply creates action staging on the destination filesystem. This path is for
# local tooling/diagnostics and should share a filesystem with destination roots.
staging = "~/Libraries/Kavita/.staging"
ignore = []
# database = "~/.local/state/kavita-ingest/state.sqlite3"

[source]
# move_after_verify removes incoming originals only after verified destination commit.
# Use preserve for the first controlled trial.
lifecycle = "preserve"
# archive_root = "~/Archives/Reading-Originals"

[cbr]
convert_to_cbz = true

[naming]
book_folder = "{series_or_title}"
book = "{title}"
book_series = "{series} - {number} - {title}"
comic_folder = "{series}"
comic = "{series} - {number} - {title}"
comic_specials_subfolder = true

[sequence]
integer_padding = 3

[permissions]
# Applied to newly published files and directories. Existing directories are unchanged.
file_mode = "0644"
directory_mode = "0755"

[archive]
max_entries = 5000
max_entry_bytes = 536870912
max_total_bytes = 4294967296
max_path_depth = 20
max_ratio = 1000.0

[providers]
offline = false
cache_ttl_seconds = 604800
timeout_seconds = 15

[providers.open_library]
enabled = true
# contact = "you@example.com"
identified_interval = 0.4
unidentified_interval = 1.25

[providers.google_books]
enabled = true
min_interval = 0.25
# API key is optional; use GOOGLE_BOOKS_API_KEY when configured.

[providers.comic_vine]
enabled = false
max_requests = 180
window_seconds = 3600
min_interval = 1.25
# Comic Vine network access requires COMIC_VINE_API_KEY.

[matching]
# Eligibility is analytical only; every accepted identity still needs explicit approval.
eligible_score = 92
eligible_margin = 12
classification_confidence = 0.90

[cache]
# Provider cache TTL is configured in [providers].

[logging]
level = "INFO"
'''
