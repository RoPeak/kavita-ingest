from __future__ import annotations

from pathlib import Path

from ..archive_safety import ArchiveLimits
from ..domain import InspectionResult, InspectionStatus, SourceFormat
from .comic import inspect_cbr, inspect_cbz
from .epub import inspect_epub
from .pdf import inspect_pdf


def inspect(path: Path, format_: SourceFormat, limits: ArchiveLimits) -> InspectionResult:
    if format_ is SourceFormat.EPUB:
        return inspect_epub(path, limits)
    if format_ is SourceFormat.CBZ:
        return inspect_cbz(path, limits)
    if format_ is SourceFormat.CBR:
        return inspect_cbr(path, limits)
    if format_ is SourceFormat.PDF:
        return inspect_pdf(path)
    return InspectionResult(
        InspectionStatus.FAILED,
        format_,
        error_code="signature_mismatch",
        error_message="The source extension is supported but its content signature is not.",
    )
