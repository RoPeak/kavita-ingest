from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import rarfile

from ..archive_safety import ArchiveLimits, validate_inventory
from ..comicinfo import COMICINFO_PROFILE_STRICT
from .comic import write_cbz_metadata
from .common import VerificationResult


def repack_cbr_to_cbz(
    source: Path,
    destination: Path,
    *,
    set_fields: dict[str, object],
    clear_fields: tuple[str, ...] = (),
    limits: ArchiveLimits | None = None,
    comicinfo_profile: str = COMICINFO_PROFILE_STRICT,
) -> VerificationResult:
    resolved_limits = limits or ArchiveLimits()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".raw.cbz", dir=destination.parent
    )
    os.close(fd)
    raw_cbz = Path(name)
    metadata_cbz = raw_cbz.with_suffix(".metadata.cbz")
    try:
        with rarfile.RarFile(source) as archive:
            if archive.needs_password():
                raise ValueError("encrypted RAR/CBR archives are unsupported")
            if archive.volumelist() != [str(source)]:
                raise ValueError("multi-volume RAR/CBR archives are unsupported")
            members = archive.infolist()
            links = {member.filename for member in members if member.is_symlink()}
            encrypted = {member.filename for member in members if member.needs_password()}
            validate_inventory(
                members, resolved_limits, link_names=links, encrypted_names=encrypted
            )
            with zipfile.ZipFile(raw_cbz, "w", compression=zipfile.ZIP_DEFLATED) as target:
                for member in members:
                    if member.is_dir():
                        continue
                    with (
                        archive.open(member) as source_stream,
                        target.open(member.filename.replace("\\", "/"), "w") as target_stream,
                    ):
                        shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
        result = write_cbz_metadata(
            raw_cbz,
            metadata_cbz,
            set_fields=set_fields,
            clear_fields=clear_fields,
            comicinfo_profile=comicinfo_profile,
        )
        shutil.move(metadata_cbz, destination)
        return result
    finally:
        raw_cbz.unlink(missing_ok=True)
        metadata_cbz.unlink(missing_ok=True)
