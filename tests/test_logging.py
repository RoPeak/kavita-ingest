from __future__ import annotations

from pathlib import Path

import pytest

from kavita_ingest.logging_config import configure_logging


def test_file_logging_failure_falls_back_to_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_parent = tmp_path / "blocked"

    def reject(*args: object, **kwargs: object) -> None:
        raise OSError("read-only fixture")

    monkeypatch.setattr(Path, "mkdir", reject)
    configure_logging("INFO", blocked_parent / "app.log")
    assert "file logging unavailable: read-only fixture" in capsys.readouterr().err
