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


def checks(config: AppConfig, paths: AppPaths) -> tuple[Check, ...]:
    output = [
        Check("config", "OK" if paths.config_file.exists() else "INFO", str(paths.config_file)),
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
            else "not required until metadata-writing milestones",
        )
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
