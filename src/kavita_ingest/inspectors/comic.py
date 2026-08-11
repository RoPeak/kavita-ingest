from __future__ import annotations

import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any

import rarfile

from ..archive_safety import ArchiveLimits, UnsafeArchive, validate_inventory
from ..comicinfo import ComicInfoError, read_comicinfo
from ..discovery import is_multivolume_path
from ..domain import Evidence, InspectionResult, InspectionStatus, SourceFormat

COMICINFO_MAX_BYTES = 2 * 1024 * 1024


def inspect_cbz(path: Path, limits: ArchiveLimits) -> InspectionResult:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            links = {
                item.filename
                for item in members
                if (item.external_attr >> 16) & 0o170000 == 0o120000
            }
            validate_inventory(
                members,
                limits,
                link_names=links,
                encrypted_names={i.filename for i in members if i.flag_bits & 1},
            )
            return _comic_result(
                SourceFormat.CBZ,
                [_inventory_item(item, item.filename in links) for item in members],
                _comicinfo_bytes_zip(archive, members),
            )
    except (OSError, ValueError, zipfile.BadZipFile, UnsafeArchive, ComicInfoError) as exc:
        return _failed(SourceFormat.CBZ, "invalid_cbz", exc)


def inspect_cbr(path: Path, limits: ArchiveLimits) -> InspectionResult:
    if is_multivolume_path(path):
        return InspectionResult(
            InspectionStatus.BLOCKED,
            SourceFormat.CBR,
            error_code="multivolume_rar_unsupported",
            error_message=(
                "Multi-volume RAR sets are deferred; MVP ingestion requires one logical comic "
                "archive to be one physical CBR/RAR file. The source was left untouched."
            ),
        )
    if not _unrar_available():
        return InspectionResult(
            InspectionStatus.BLOCKED,
            SourceFormat.CBR,
            error_code="unrar_unavailable",
            error_message="A diagnosed unrar backend is required.",
        )
    try:
        with rarfile.RarFile(path) as archive:
            if archive.needs_password():
                raise UnsafeArchive("encrypted RAR archives are unsupported")
            members = archive.infolist()
            links = {item.filename for item in members if item.is_symlink()}
            encrypted = {item.filename for item in members if item.needs_password()}
            validate_inventory(members, limits, link_names=links, encrypted_names=encrypted)
            comicinfo = _comicinfo_bytes_rar(archive, members)
            return _comic_result(
                SourceFormat.CBR,
                [_inventory_item(item, item.filename in links) for item in members],
                comicinfo,
            )
    except (OSError, ValueError, rarfile.Error, UnsafeArchive, ComicInfoError) as exc:
        return _failed(SourceFormat.CBR, "invalid_cbr", exc)


def _comic_result(
    format_: SourceFormat, inventory: list[dict[str, object]], comicinfo: bytes | None
) -> InspectionResult:
    names = [str(item["path"]) for item in inventory]
    images = [
        name
        for name in names
        if Path(name).suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    ]
    metadata: dict[str, object] = {
        "entry_count": len(names),
        "page_count": len(images),
        "inventory": inventory,
    }
    warnings: list[str] = []
    evidence: list[Evidence] = []
    if comicinfo is not None:
        document = read_comicinfo(comicinfo)
        metadata["comicinfo"] = document.metadata
        metadata["comicinfo_schema_valid"] = document.schema_valid
        if not document.schema_valid:
            warnings.append("ComicInfo.xml does not validate against the pinned 2.1 schema")
        evidence.extend(
            Evidence(key.casefold(), str(value), str(value), "comicinfo", 0.99)
            for key, value in document.metadata.items()
        )
    return InspectionResult(
        InspectionStatus.OK, format_, metadata, tuple(evidence), tuple(warnings)
    )


def _comicinfo_bytes_zip(archive: zipfile.ZipFile, members: list[zipfile.ZipInfo]) -> bytes | None:
    matches = [item for item in members if Path(item.filename).name.casefold() == "comicinfo.xml"]
    if len(matches) > 1:
        raise UnsafeArchive("multiple ComicInfo.xml entries are ambiguous")
    if matches and matches[0].file_size > COMICINFO_MAX_BYTES:
        raise UnsafeArchive("ComicInfo.xml exceeds the metadata size limit")
    return archive.read(matches[0]) if matches else None


def _comicinfo_bytes_rar(archive: rarfile.RarFile, members: list[rarfile.RarInfo]) -> bytes | None:
    matches = [item for item in members if Path(item.filename).name.casefold() == "comicinfo.xml"]
    if len(matches) > 1:
        raise UnsafeArchive("multiple ComicInfo.xml entries are ambiguous")
    if matches and matches[0].file_size > COMICINFO_MAX_BYTES:
        raise UnsafeArchive("ComicInfo.xml exceeds the metadata size limit")
    return archive.read(matches[0]) if matches else None


def _failed(format_: SourceFormat, code: str, exc: Exception) -> InspectionResult:
    return InspectionResult(
        InspectionStatus.FAILED,
        format_,
        error_code=code,
        error_message=f"{type(exc).__name__}: {exc}",
    )


def _inventory_item(item: Any, is_link: bool) -> dict[str, object]:
    return {
        "path": str(item.filename).replace("\\", "/"),
        "size": int(item.file_size),
        "compressed_size": int(item.compress_size),
        "is_directory": bool(item.is_dir()),
        "is_link": is_link,
    }


@lru_cache(maxsize=1)
def _unrar_available() -> bool:
    try:
        rarfile.tool_setup(
            unrar=True,
            unar=False,
            bsdtar=False,
            sevenzip=False,
            sevenzip2=False,
            force=True,
        )
    except rarfile.RarCannotExec:
        return False
    return True
