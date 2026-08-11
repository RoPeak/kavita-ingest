from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from pathlib import Path

from ..comicinfo import patch_comicinfo, read_comicinfo
from .common import VerificationResult

COMICINFO_NAME = "ComicInfo.xml"


def write_cbz_metadata(
    source: Path,
    destination: Path,
    *,
    set_fields: dict[str, object],
    clear_fields: tuple[str, ...] = (),
) -> VerificationResult:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if archive.testzip() is not None:
            raise ValueError("source CBZ failed CRC validation")
        payloads = {info.filename: archive.read(info.filename) for info in infos}
    comic_names = [name for name in payloads if name.casefold() == COMICINFO_NAME.casefold()]
    if len(comic_names) > 1:
        raise ValueError("CBZ contains duplicate or case-colliding ComicInfo.xml entries")
    original = payloads[comic_names[0]] if comic_names else b"<ComicInfo/>"
    patched = patch_comicinfo(original, set_fields=set_fields, clear_fields=clear_fields)
    if comic_names:
        payloads[comic_names[0]] = patched
    else:
        infos.append(_comicinfo_zipinfo())
        payloads[COMICINFO_NAME] = patched
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".cbz", dir=destination.parent
    )
    os.close(fd)
    staged = Path(name)
    try:
        with zipfile.ZipFile(staged, "w") as target:
            for info in infos:
                target.writestr(info, payloads[info.filename])
        result = verify_cbz(source, staged, set_fields, clear_fields)
        result.require_valid()
        os.replace(staged, destination)
        return result
    finally:
        staged.unlink(missing_ok=True)


def verify_cbz(
    source: Path,
    candidate: Path,
    set_fields: dict[str, object],
    clear_fields: tuple[str, ...] = (),
) -> VerificationResult:
    errors: list[str] = []
    try:
        source_payloads = _payload_hashes(source)
        candidate_payloads = _payload_hashes(candidate)
        source_payloads.pop(COMICINFO_NAME.casefold(), None)
        candidate_payloads.pop(COMICINFO_NAME.casefold(), None)
        if source_payloads != candidate_payloads:
            errors.append("comic page payloads or order changed")
        with zipfile.ZipFile(candidate) as archive:
            if archive.testzip() is not None:
                errors.append("candidate CBZ failed CRC validation")
            names = [
                name for name in archive.namelist() if name.casefold() == COMICINFO_NAME.casefold()
            ]
            if len(names) != 1:
                errors.append("candidate must contain exactly one ComicInfo.xml")
            else:
                document = read_comicinfo(archive.read(names[0]), require_schema=True)
                for field, expected in set_fields.items():
                    if document.metadata.get(field) != str(expected):
                        errors.append(f"ComicInfo {field} read-back mismatch")
                for field in clear_fields:
                    if field in document.metadata:
                        errors.append(f"ComicInfo {field} was not cleared")
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        errors.append(str(exc))
    return VerificationResult(
        not errors,
        ("zip_crc", "payload_hashes_and_order", "comicinfo_schema", "metadata_readback"),
        tuple(errors),
    )


def _payload_hashes(path: Path) -> dict[str, tuple[int, str]]:
    with zipfile.ZipFile(path) as archive:
        return {
            info.filename.casefold(): (
                index,
                hashlib.sha256(archive.read(info.filename)).hexdigest(),
            )
            for index, info in enumerate(archive.infolist())
            if not info.is_dir()
        }


def _comicinfo_zipinfo() -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(COMICINFO_NAME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info
