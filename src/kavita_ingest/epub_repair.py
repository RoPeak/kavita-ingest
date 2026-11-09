from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .archive_safety import ArchiveLimits
from .epub_validation import MIMETYPE, EpubDisposition, validate_epub_container


@dataclass(frozen=True, slots=True)
class EpubRepairResult:
    source: str
    output: str
    source_sha256: str
    output_sha256: str
    package_path: str
    source_findings: tuple[str, ...]
    member_hashes: dict[str, str]
    report: str


def repair_epub(
    source: Path,
    output: Path,
    limits: ArchiveLimits,
    *,
    dry_run: bool,
) -> EpubRepairResult | None:
    """Rebuild only repairable OCF packaging into a new, explicit path."""
    source_input = source.expanduser().absolute()
    if source_input.is_symlink():
        raise ValueError("EPUB source must not be a symlink")
    source = source_input.resolve(strict=True)
    source_identity = _identity(source)
    output = output.expanduser().absolute()
    if output == source:
        raise ValueError("repair output must differ from the source")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"repair output already exists: {output}")
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError("repair output directory must be an existing non-symlink directory")
    validation = validate_epub_container(source, limits)
    if validation.disposition is EpubDisposition.VALID:
        raise ValueError("EPUB is already valid; no repair is needed")
    if validation.disposition is not EpubDisposition.REPAIRABLE:
        detail = "; ".join(item.message for item in validation.findings)
        raise ValueError(f"EPUB repair is refused: {detail}")
    if dry_run:
        return None

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".repairing", dir=output.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    report = output.with_suffix(output.suffix + ".repair.json")
    if report.exists() or report.is_symlink():
        temporary.unlink(missing_ok=True)
        raise FileExistsError(f"repair report already exists: {report}")
    try:
        report_fd, report_temporary_name = tempfile.mkstemp(
            prefix=f".{report.name}.", suffix=".tmp", dir=output.parent
        )
        os.close(report_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    report_temporary = Path(report_temporary_name)
    published_output = False
    published_report = False
    try:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(temporary, "w") as rebuilt:
            rebuilt.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
            for info in original.infolist():
                if info.filename == "mimetype":
                    continue
                rebuilt.writestr(info, original.read(info))
        _fsync_path(temporary)
        repaired = validate_epub_container(temporary, limits)
        repaired.require_valid()
        expected_hashes = dict(validation.member_hashes)
        expected_hashes["mimetype"] = hashlib.sha256(MIMETYPE).hexdigest()
        if repaired.member_hashes != expected_hashes:
            raise ValueError("repaired EPUB changed publication member content")
        source_sha256, current_source_identity = _stable_sha256(source)
        if current_source_identity != source_identity:
            raise ValueError("EPUB source changed during repair")
        result = EpubRepairResult(
            source=str(source),
            output=str(output),
            source_sha256=source_sha256,
            output_sha256=_sha256(temporary),
            package_path=str(repaired.package_path),
            source_findings=tuple(item.code for item in validation.findings),
            member_hashes=repaired.member_hashes,
            report=str(report),
        )
        report_temporary.write_text(
            json.dumps(
                {
                    "report_schema": "kavita-ingest-epub-repair-v1",
                    **asdict(result),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.chmod(report_temporary, 0o600)
        _fsync_path(report_temporary)
        os.link(temporary, output)
        published_output = True
        os.link(report_temporary, report)
        published_report = True
        _fsync_directory(output.parent)
        return result
    except Exception:
        if published_report:
            report.unlink(missing_ok=True)
        if published_output:
            output.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        report_temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> tuple[int, int, int, int]:
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _stable_sha256(path: Path) -> tuple[str, tuple[int, int, int, int]]:
    before = _identity(path)
    digest = _sha256(path)
    after = _identity(path)
    if before != after:
        raise ValueError("EPUB source changed while hashing")
    return digest, after


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
