from __future__ import annotations

import subprocess

import pytest

from kavita_ingest.calibre import (
    calibre_version_is_safe,
    calibre_version_tuple,
    require_safe_calibre_executable,
    safe_calibre_environment,
)


def test_calibre_security_floor() -> None:
    assert calibre_version_tuple("ebook-meta (calibre 9.13)") == (
        9,
        13,
        0,
    )

    assert calibre_version_is_safe("9.12.0")

    assert calibre_version_is_safe("9.13")

    assert not calibre_version_is_safe("9.11.0")

    assert not calibre_version_is_safe("7.6")


def test_calibre_subprocess_disables_python_templates() -> None:
    environment = safe_calibre_environment(
        {
            "PATH": "/example",
            "CALIBRE_ALLOW_PYTHON_TEMPLATES": "1",
        }
    )

    assert environment["PATH"] == "/example"

    assert environment["CALIBRE_ALLOW_PYTHON_TEMPLATES"] == "0"


def test_calibre_version_probe_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_safe_calibre_executable.cache_clear()

    def timeout(
        *args: object,
        **kwargs: object,
    ) -> None:
        raise subprocess.TimeoutExpired(
            cmd=["ebook-meta", "--version"],
            timeout=10,
        )

    monkeypatch.setattr(
        "kavita_ingest.calibre.subprocess.run",
        timeout,
    )

    with pytest.raises(
        ValueError,
        match="timed out querying Calibre helper version",
    ):
        require_safe_calibre_executable("ebook-meta")

    require_safe_calibre_executable.cache_clear()
