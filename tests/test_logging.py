from __future__ import annotations

from pathlib import Path

import pytest

from kavita_ingest.logging_config import RedactingFilter, configure_logging


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


def test_logging_redacts_known_secrets_headers_and_query_parameters(
    tmp_path: Path,
) -> None:
    import logging

    log = tmp_path / "app.log"
    configure_logging("INFO", log, secrets=("private-provider-value",))
    logging.getLogger("fixture").info(
        "secret=%s Authorization: Bearer abc123 https://example.test/?api_key=query-secret",
        "private-provider-value",
    )
    text = log.read_text(encoding="utf-8")
    assert "private-provider-value" not in text
    assert "abc123" not in text
    assert "query-secret" not in text
    assert text.count("[REDACTED]") == 3


def test_redacting_filter_never_changes_log_control_flow() -> None:
    import logging

    record = logging.LogRecord("test", logging.INFO, __file__, 1, "ordinary", (), None)
    assert RedactingFilter().filter(record)
