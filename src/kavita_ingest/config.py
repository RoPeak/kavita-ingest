from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import AppPaths


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
    )


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{key}] must be a TOML table")
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
