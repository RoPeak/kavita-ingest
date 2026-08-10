from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from lxml import etree

COMICINFO_FIELD_ORDER = (
    "Title", "Series", "Number", "Count", "Volume", "AlternateSeries", "AlternateNumber",
    "AlternateCount", "Summary", "Notes", "Year", "Month", "Day", "Writer", "Penciller",
    "Inker", "Colorist", "Letterer", "CoverArtist", "Editor", "Translator", "Publisher",
    "Imprint", "Genre", "Tags", "Web", "PageCount", "LanguageISO", "Format", "BlackAndWhite",
    "Manga", "Characters", "Teams", "Locations", "ScanInformation", "StoryArc",
    "StoryArcNumber", "SeriesGroup", "AgeRating", "Pages", "CommunityRating",
    "MainCharacterOrTeam", "Review", "GTIN",
)
COMICINFO_FIELD_INDEX = {name: index for index, name in enumerate(COMICINFO_FIELD_ORDER)}


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 10_000
    max_entry_size: int = 2 * 1024**3
    max_total_size: int = 20 * 1024**3
    max_path_depth: int = 32
    max_compression_ratio: float = 1_000.0


def normalized_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"unsafe archive path: {name!r}")
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe archive path: {name!r}")
    return path.as_posix()


def validate_inventory(infos: Iterable[object], limits: ArchiveLimits = ArchiveLimits()) -> list[str]:
    entries = list(infos)
    if len(entries) > limits.max_entries:
        raise ValueError("archive entry-count limit exceeded")

    names: list[str] = []
    folded: set[str] = set()
    total = 0
    for info in entries:
        name = normalized_member_name(str(getattr(info, "filename")))
        if len(PurePosixPath(name).parts) > limits.max_path_depth:
            raise ValueError(f"archive path-depth limit exceeded: {name}")
        key = name.casefold()
        if key in folded:
            raise ValueError(f"duplicate or case-colliding archive path: {name}")
        folded.add(key)

        size = int(getattr(info, "file_size", 0))
        compressed = int(getattr(info, "compress_size", 0))
        if bool(getattr(info, "is_symlink")()):
            raise ValueError(f"archive links are not supported: {name}")
        if size > limits.max_entry_size:
            raise ValueError(f"archive entry-size limit exceeded: {name}")
        total += size
        if total > limits.max_total_size:
            raise ValueError("archive total-size limit exceeded")
        if compressed and size / compressed > limits.max_compression_ratio:
            raise ValueError(f"archive compression-ratio limit exceeded: {name}")
        names.append(name)
    return names


def safe_extract_regular_files(rar: object, destination: Path) -> dict[str, str]:
    infos = list(getattr(rar, "infolist")())
    names = validate_inventory(infos)
    destination.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for info, name in zip(infos, names, strict=True):
        if bool(getattr(info, "is_dir")()):
            continue
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with getattr(rar, "open")(info) as source, target.open("xb") as output:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        hashes[name] = digest.hexdigest()
    return hashes


def patch_comicinfo(xml: bytes, owned_fields: dict[str, str | int | None]) -> bytes:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
    root = etree.fromstring(xml, parser=parser)
    if root.tag != "ComicInfo":
        raise ValueError("ComicInfo root element required")
    for field, value in owned_fields.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", field):
            raise ValueError(f"invalid ComicInfo field name: {field}")
        matches = root.findall(field)
        if value is None:
            for node in matches:
                root.remove(node)
            continue
        if matches:
            node = matches[0]
        else:
            node = etree.Element(field)
            field_index = COMICINFO_FIELD_INDEX.get(field, len(COMICINFO_FIELD_ORDER))
            insertion_index = len(root)
            for index, child in enumerate(root):
                child_index = COMICINFO_FIELD_INDEX.get(str(child.tag), len(COMICINFO_FIELD_ORDER))
                if child_index > field_index:
                    insertion_index = index
                    break
            root.insert(insertion_index, node)
        node.text = str(value)
        for duplicate in matches[1:]:
            root.remove(duplicate)
    return etree.tostring(root, encoding="utf-8", xml_declaration=True)
