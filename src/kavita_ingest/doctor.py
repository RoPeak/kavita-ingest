from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import lxml
import pikepdf
import rarfile

from . import __version__
from .config import AppConfig
from .filesystem import LinuxFilesystem
from .locking import LockUnavailable, ProcessLock, lock_path
from .paths import AppPaths


@dataclass(frozen=True, slots=True)
class Check:
    category: str
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def checks(
    config: AppConfig,
    paths: AppPaths,
    config_file: Path | None = None,
) -> tuple[Check, ...]:
    selected_config = config_file or paths.config_file
    output = [
        Check("application", "version", "OK", __version__),
        Check(
            "application",
            "config",
            "OK" if selected_config.exists() else "INFO",
            str(selected_config),
        ),
        Check("application", "database", *_database_status(config.database_path)),
        Check("application", "migration-backup", *_backup_status(config.database_path)),
        Check("application", "process-lock", *_lock_status(config.database_path)),
        Check("runtime", "python", "OK", sys.version.split()[0]),
        Check("helpers", "lxml", "OK", lxml.__version__),
        Check("helpers", "pikepdf", "OK", pikepdf.__version__),
        Check("helpers", "rarfile", "OK", rarfile.__version__),
    ]
    output.extend(_path_checks(config))
    output.extend(_planning_checks(config))
    output.extend(_helper_checks())
    output.extend(_format_checks())
    output.extend(_provider_checks(config))
    output.extend(_provider_database_checks(config.database_path))
    output.extend(_apply_database_checks(config.database_path))
    output.append(
        Check("providers", "provider-live", "INFO", "not tested; doctor is network-independent")
    )
    return tuple(output)


def _database_status(path: Path | None) -> tuple[str, str]:
    if path is None:
        return "BLOCKED", "no database path configured"
    if not path.exists():
        return "INFO", f"not created yet: {path}"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            migration = connection.execute(
                "SELECT max(version) FROM schema_migrations"
            ).fetchone()
        if not result or result[0] != "ok":
            return "BLOCKED", "integrity check failed"
        return "OK", f"{path}; schema={migration[0] or 0}; integrity=ok"
    except sqlite3.Error as exc:
        return "BLOCKED", str(exc)


def _backup_status(path: Path | None) -> tuple[str, str]:
    if path is None:
        return "BLOCKED", "database path is not configured"
    parent = _existing_parent(path.parent)
    writable = os.access(parent, os.W_OK | os.X_OK)
    return (
        "OK" if writable else "BLOCKED",
        f"timestamped validated backups can be created via {parent}"
        if writable
        else f"database parent is not writable: {parent}",
    )


def _lock_status(path: Path | None) -> tuple[str, str]:
    if path is None:
        return "BLOCKED", "database path is not configured"
    try:
        with ProcessLock(lock_path(path)):
            pass
    except (LockUnavailable, OSError) as exc:
        return "BLOCKED", str(exc)
    return "OK", f"advisory lock available: {lock_path(path)}"


def _path_checks(config: AppConfig) -> list[Check]:
    output: list[Check] = []
    for index, incoming in enumerate(config.incoming_roots, 1):
        output.append(_path_check("paths", f"incoming-{index}", incoming))
    for name, root in (("books", config.books_root), ("comics", config.comics_root)):
        if root is None:
            output.append(Check("paths", name, "BLOCKED", "not configured"))
            continue
        output.append(_path_check("paths", name, root))
        probe = LinuxFilesystem().probe_no_clobber(root)
        output.append(
            Check(
                "publication",
                f"{name}-no-clobber",
                "OK" if probe.supported else "BLOCKED",
                probe.detail,
            )
        )
    if config.staging_root is None:
        output.append(
            Check(
                "paths",
                "staging",
                "INFO",
                "not configured; apply always stages under each destination root",
            )
        )
    else:
        output.append(_path_check("paths", "staging", config.staging_root))
        output.append(
            Check(
                "paths",
                "staging-semantics",
                "OK",
                "apply ignores cross-filesystem staging and stages beside each destination",
            )
        )
    return output


def _planning_checks(config: AppConfig) -> list[Check]:
    naming = config.naming_policy()
    archive_root = (
        str(config.source_archive_root)
        if config.source_archive_root is not None
        else "not configured"
    )
    return [
        Check(
            "planning",
            "source-lifecycle",
            "OK",
            f"{config.source_lifecycle}; archive_root={archive_root}",
        ),
        Check(
            "planning",
            "cbr-conversion",
            "OK" if config.cbr_conversion_enabled else "INFO",
            "CBR -> CBZ enabled"
            if config.cbr_conversion_enabled
            else "disabled; CBR items cannot enter an actionable plan",
        ),
        Check(
            "planning",
            "naming",
            "OK",
            f"book={naming.book_file!r}; book_series={naming.book_series_file!r}; "
            f"comic={naming.comic_file!r}; integer_padding={naming.integer_padding}; "
            f"specials_subfolder={str(naming.comic_specials_subfolder).lower()}",
        ),
        Check(
            "planning",
            "archive-limits",
            "OK",
            f"entries={config.archive_entry_limit}; entry_bytes={config.archive_entry_size_limit}; "
            f"total_bytes={config.archive_total_size_limit}; "
            f"depth={config.archive_path_depth_limit}; ratio={config.archive_ratio_limit:g}",
        ),
    ]


def _path_check(category: str, name: str, path: Path) -> Check:
    if not path.exists():
        return Check(category, name, "BLOCKED", f"path does not exist: {path}")
    if not path.is_dir():
        return Check(category, name, "BLOCKED", f"not a directory: {path}")
    usage = shutil.disk_usage(path)
    writable = os.access(path, os.W_OK | os.X_OK)
    return Check(
        category,
        name,
        "OK" if writable else "BLOCKED",
        f"{path}; writable={str(writable).lower()}; free={_human_bytes(usage.free)}; "
        f"device={path.stat().st_dev}",
    )


def _helper_checks() -> list[Check]:
    unrar = shutil.which("unrar")
    calibre = shutil.which("ebook-meta")
    return [
        Check(
            "helpers",
            "unrar",
            "OK" if unrar else "BLOCKED",
            _tool_version(unrar, arguments=("-?",), version_hint="UNRAR")
            if unrar
            else "not found; CBR inspection/repacking is unavailable",
        ),
        Check(
            "helpers",
            "ebook-meta",
            "OK" if calibre else "BLOCKED",
            _tool_version(calibre, version_hint="ebook-meta (calibre")
            if calibre
            else "not found; Calibre-owned EPUB metadata writes are unavailable",
        ),
    ]


def _format_checks() -> list[Check]:
    calibre = shutil.which("ebook-meta") is not None
    unrar = shutil.which("unrar") is not None
    return [
        Check(
            "formats",
            "EPUB",
            "OK" if calibre else "BLOCKED",
            "inspect/write; contributor roles use verified narrow OPF patching",
        ),
        Check("formats", "CBZ", "OK", "inspect/write ComicInfo 2.1"),
        Check(
            "formats",
            "CBR-RAR3",
            "OK" if unrar else "BLOCKED",
            "single-volume inspect/repack; encrypted and linked entries blocked",
        ),
        Check(
            "formats",
            "CBR-RAR5",
            "OK" if unrar else "BLOCKED",
            "single-volume inspect/repack; multi-volume and encrypted archives blocked",
        ),
        Check(
            "formats",
            "PDF",
            "OK",
            "inspect/write unsigned and unencrypted PDFs; encrypted/signed writes blocked",
        ),
    ]


def _provider_checks(config: AppConfig) -> list[Check]:
    contact = config.providers.open_library_contact
    return [
        Check(
            "providers",
            "open-library",
            "INFO" if not config.providers.open_library_enabled else ("OK" if contact else "INFO"),
            "disabled"
            if not config.providers.open_library_enabled
            else (
                "identified contact configured" if contact else "no key required; contact missing"
            ),
        ),
        Check(
            "providers",
            "google-books",
            "INFO"
            if not config.providers.google_books_enabled
            or not config.providers.google_books_api_key
            else "OK",
            "disabled"
            if not config.providers.google_books_enabled
            else (
                "API key present"
                if config.providers.google_books_api_key
                else "anonymous access"
            ),
        ),
        Check(
            "providers",
            "comic-vine",
            "INFO"
            if not config.providers.comic_vine_enabled
            else ("OK" if config.providers.comic_vine_api_key else "BLOCKED"),
            "disabled"
            if not config.providers.comic_vine_enabled
            else (
                "API key present"
                if config.providers.comic_vine_api_key
                else "COMIC_VINE_API_KEY is missing"
            ),
        ),
    ]


def _tool_version(
    executable: str,
    *,
    arguments: tuple[str, ...] = ("--version",),
    version_hint: str | None = None,
) -> str:
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        lines = [
            line.strip()
            for line in f"{result.stdout}\n{result.stderr}".splitlines()
            if line.strip()
        ]
        if version_hint:
            match = next(
                (line for line in lines if version_hint.casefold() in line.casefold()), None
            )
            if match:
                return match
        return lines[0] if lines else executable
    except (OSError, subprocess.SubprocessError):
        return executable


def _provider_database_checks(path: Path | None) -> list[Check]:
    if path is None or not path.exists():
        return [Check("providers", "cache", "INFO", "state database has not been created")]
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "provider_cache" not in tables:
                return [Check("providers", "cache", "INFO", "provider migration not applied")]
            cache = connection.execute(
                "SELECT count(*), sum(CASE WHEN expires_at <= unixepoch() THEN 1 ELSE 0 END) "
                "FROM provider_cache"
            ).fetchone()
            reservations = connection.execute(
                "SELECT count(*) FROM provider_rate_reservations "
                "WHERE reserved_at > unixepoch() - 3600"
            ).fetchone()[0]
        return [
            Check("providers", "cache", "OK", f"{cache[0]} entries; {cache[1] or 0} expired"),
            Check(
                "providers",
                "rate-limits",
                "OK",
                f"{reservations} persisted reservations in last hour",
            ),
        ]
    except sqlite3.Error as exc:
        return [Check("providers", "cache", "BLOCKED", str(exc))]


def _apply_database_checks(path: Path | None) -> list[Check]:
    if path is None or not path.exists():
        return [Check("application", "apply-state", "INFO", "state database not created")]
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "apply_runs" not in tables:
                return [
                    Check("application", "apply-state", "INFO", "apply migration not applied")
                ]
            active = connection.execute(
                "SELECT count(*) FROM apply_runs WHERE status IN "
                "('preflighting', 'running', 'recovery_required')"
            ).fetchone()[0]
            attention = connection.execute(
                "SELECT count(*) FROM apply_items WHERE state NOT IN ('complete', 'stale')"
            ).fetchone()[0]
        return [
            Check(
                "application",
                "apply-state",
                "BLOCKED" if attention else "OK",
                f"{active} active/recoverable run(s); {attention} item(s) require attention",
            )
        ]
    except sqlite3.Error as exc:
        return [Check("application", "apply-state", "BLOCKED", str(exc))]


def _existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"
