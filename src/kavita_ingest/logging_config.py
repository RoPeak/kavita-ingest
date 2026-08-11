from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_console_level = logging.WARNING


def set_console_verbosity(*, verbose: bool = False, debug: bool = False) -> None:
    global _console_level
    _console_level = logging.DEBUG if debug else logging.INFO if verbose else logging.WARNING


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        for secret in self.secrets:
            rendered = rendered.replace(secret, "[REDACTED]")
        rendered = re.sub(
            r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s,;]+",
            r"\1[REDACTED]",
            rendered,
        )
        rendered = re.sub(
            r"(?i)([?&](?:api_key|key|token|access_token)=)[^&\s]+",
            r"\1[REDACTED]",
            rendered,
        )
        record.msg = rendered
        record.args = ()
        return True


def configure_logging(
    level: str,
    log_file: Path | None = None,
    *,
    secrets: tuple[str, ...] = (),
) -> None:
    console = logging.StreamHandler()
    console.setLevel(_console_level)
    handlers: list[logging.Handler] = [console]
    file_error: OSError | None = None
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=2 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
            handlers.append(file_handler)
        except OSError as exc:
            file_error = exc
    redactor = RedactingFilter(secrets)
    for handler in handlers:
        handler.addFilter(redactor)
    logging.basicConfig(
        level=min(_console_level, getattr(logging, level.upper(), logging.INFO)),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    if file_error is not None:
        logging.getLogger(__name__).warning("file logging unavailable: %s", file_error)


def provider_secrets(config: Any) -> tuple[str, ...]:
    providers = config.providers
    return tuple(
        value for value in (providers.google_books_api_key, providers.comic_vine_api_key) if value
    )
