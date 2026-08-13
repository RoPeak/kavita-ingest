from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from functools import lru_cache

MIN_SAFE_CALIBRE = (9, 12, 0)
MIN_SAFE_CALIBRE_TEXT = "9.12.0"


def calibre_version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)+", value)

    if match is None:
        raise ValueError(f"could not determine Calibre version from: {value!r}")

    parts = tuple(int(part) for part in match.group().split("."))

    return parts + (0,) * max(
        0,
        3 - len(parts),
    )


def calibre_version_is_safe(value: str) -> bool:
    return calibre_version_tuple(value) >= MIN_SAFE_CALIBRE


def require_safe_calibre_version(value: str) -> str:
    if not calibre_version_is_safe(value):
        raise ValueError(
            "unsafe Calibre version: "
            f"{value}; kavita-ingest requires calibre >= "
            f"{MIN_SAFE_CALIBRE_TEXT} for untrusted "
            "EPUB/PDF metadata"
        )

    return value


@lru_cache(maxsize=8)
def require_safe_calibre_executable(
    executable: str = "ebook-meta",
) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError as exc:
        raise ValueError(f"required Calibre helper is unavailable: {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"timed out querying Calibre helper version: {executable}") from exc

    output = (f"{result.stdout}\n{result.stderr}").strip()

    if result.returncode != 0:
        raise ValueError(f"failed to query Calibre version: {output or executable}")

    match = re.search(
        r"\d+(?:\.\d+)+",
        output,
    )

    if match is None:
        raise ValueError(f"could not determine Calibre version from: {output!r}")

    return require_safe_calibre_version(match.group())


def safe_calibre_environment(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)

    # Defence in depth in addition to requiring a patched Calibre.
    # Older vulnerable versions enabled Python templates by default.
    environment["CALIBRE_ALLOW_PYTHON_TEMPLATES"] = "0"

    return environment
