from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import lxml
import pikepdf
import rarfile

from .config import AppConfig
from .paths import AppPaths


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    detail: str


def checks(
    config: AppConfig,
    paths: AppPaths,
    config_file: Path | None = None,
) -> tuple[Check, ...]:
    selected_config = config_file or paths.config_file
    output = [
        Check("config", "OK" if selected_config.exists() else "INFO", str(selected_config)),
        Check("database", *_database_status(config.database_path)),
        Check("lxml", "OK", lxml.__version__),
        Check("pikepdf", "OK", pikepdf.__version__),
        Check("rarfile", "OK", rarfile.__version__),
    ]
    unrar = shutil.which("unrar")
    if unrar:
        output.append(
            Check("unrar", "OK", _tool_version(unrar, arguments=("-?",), version_hint="UNRAR"))
        )
    else:
        output.append(Check("unrar", "BLOCKED", "not found; CBR inspection is unavailable"))
    calibre = shutil.which("ebook-meta")
    output.append(
        Check(
            "ebook-meta",
            "OK" if calibre else "INFO",
            _tool_version(calibre, version_hint="ebook-meta (calibre")
            if calibre
            else "not found; Calibre-owned EPUB metadata writes are unavailable",
        )
    )
    contact = config.providers.open_library_contact
    output.extend(
        [
            Check(
                "open-library",
                "OK" if contact else "INFO",
                "identified contact mode" if contact else "unidentified mode at 0.8 req/s",
            ),
            Check(
                "google-books",
                "OK",
                "API key configured"
                if config.providers.google_books_api_key
                else "anonymous access configured",
            ),
            Check(
                "comic-vine",
                "OK" if config.providers.comic_vine_api_key else "BLOCKED",
                "API key configured"
                if config.providers.comic_vine_api_key
                else "COMIC_VINE_API_KEY is missing",
            ),
        ]
    )
    output.extend(_provider_database_checks(config.database_path))
    output.extend(_apply_database_checks(config.database_path))
    output.append(Check("provider-live", "INFO", "not tested; doctor is network-independent"))
    return tuple(output)


def _database_status(path: Path | None) -> tuple[str, str]:
    if path is None:
        return "BLOCKED", "no database path configured"
    if not path.exists():
        return "INFO", f"not created yet: {path}"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        return (
            ("OK", str(path))
            if result and result[0] == "ok"
            else ("BLOCKED", "integrity check failed")
        )
    except sqlite3.Error as exc:
        return "BLOCKED", str(exc)


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
        return [Check("provider-cache", "INFO", "state database has not been created")]
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "provider_cache" not in tables:
                return [Check("provider-cache", "INFO", "provider migration not applied")]
            cache = connection.execute(
                "SELECT count(*), sum(CASE WHEN expires_at <= unixepoch() THEN 1 ELSE 0 END) "
                "FROM provider_cache"
            ).fetchone()
            reservations = connection.execute(
                "SELECT count(*) FROM provider_rate_reservations "
                "WHERE reserved_at > unixepoch() - 3600"
            ).fetchone()[0]
        return [
            Check("provider-cache", "OK", f"{cache[0]} entries; {cache[1] or 0} expired"),
            Check("rate-limits", "OK", f"{reservations} persisted reservations in last hour"),
        ]
    except sqlite3.Error as exc:
        return [Check("provider-cache", "BLOCKED", str(exc))]


def _apply_database_checks(path: Path | None) -> list[Check]:
    if path is None or not path.exists():
        return [Check("apply-state", "INFO", "state database has not been created")]
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "apply_runs" not in tables:
                return [Check("apply-state", "INFO", "apply migration not applied")]
            active = connection.execute(
                "SELECT count(*) FROM apply_runs WHERE status IN "
                "('preflighting', 'running', 'recovery_required')"
            ).fetchone()[0]
            recovery = connection.execute(
                "SELECT count(*) FROM apply_items WHERE state IN "
                "('failed', 'recovery_required', 'cleanup_pending')"
            ).fetchone()[0]
        status = "BLOCKED" if recovery else "OK"
        return [
            Check(
                "apply-state",
                status,
                f"{active} active/recoverable run(s); {recovery} item(s) require attention",
            )
        ]
    except sqlite3.Error as exc:
        return [Check("apply-state", "BLOCKED", str(exc))]
