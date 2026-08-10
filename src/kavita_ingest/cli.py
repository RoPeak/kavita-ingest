from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from .config import load_config
from .doctor import checks
from .logging_config import configure_logging
from .paths import AppPaths
from .scanner import scan as run_scan

app = typer.Typer(help="Inspect and classify reading-media sources without modifying them.")


@app.command()
def doctor(
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML configuration path.")
    ] = None,
) -> None:
    """Report current paths and external inspection capabilities."""
    paths = AppPaths.default()
    settings = load_config(config, paths)
    configure_logging(settings.log_level)
    for check in checks(settings, paths):
        typer.echo(f"{check.status:7} {check.name:12} {check.detail}")


@app.command()
def scan(
    root: Annotated[Path, typer.Argument(exists=True, file_okay=False, resolve_path=True)],
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML configuration path.")
    ] = None,
    no_persist: Annotated[
        bool, typer.Option("--no-persist", help="Do not write scan state.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Recursively inspect and classify supported files under ROOT."""
    settings = load_config(config)
    configure_logging(
        settings.log_level,
        AppPaths.default().log_file if not no_persist else None,
    )
    results = run_scan(root, settings, persist=not no_persist)
    if as_json:
        typer.echo(json.dumps([asdict(item) for item in results], indent=2, default=str))
        return
    for item in results:
        classification = item.classification
        marker = "?" if classification.ambiguous else " "
        typer.echo(
            f"{marker} {classification.kind.value:7} {classification.subtype:18} "
            f"{classification.confidence:.2f}  {item.source.path}"
        )
        if item.inspection.error_message:
            typer.echo(f"    {item.inspection.status.value}: {item.inspection.error_message}")
    typer.echo(f"Scanned {len(results)} supported source(s); source files were not modified.")


if __name__ == "__main__":
    app()
