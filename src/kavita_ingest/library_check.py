from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .archive_safety import ArchiveLimits
from .config import AppConfig
from .discovery import SUPPORTED_EXTENSIONS, detect_signature
from .domain import (
    Classification,
    InspectionResult,
    InspectionStatus,
    MediaKind,
    SequenceNumber,
    SourceFormat,
)
from .inspectors import inspect
from .naming import render_component, render_sequence
from .parsing import classify

Severity = Literal["error", "warning", "info"]
LibraryKind = Literal["books", "comics"]


@dataclass(frozen=True, slots=True)
class LibraryScope:
    kind: LibraryKind
    library_root: Path
    scan_root: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "library_root": str(self.library_root),
            "scan_root": str(self.scan_root),
        }


@dataclass(frozen=True, slots=True)
class LibraryFinding:
    severity: Severity
    code: str
    path: Path
    message: str
    expected: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": str(self.path),
            "message": self.message,
            "expected": self.expected,
        }


@dataclass(frozen=True, slots=True)
class CheckedMedia:
    path: Path
    kind: LibraryKind
    canonical: bool

    def to_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "kind": self.kind, "canonical": self.canonical}


@dataclass(frozen=True, slots=True)
class LibraryCheckResult:
    requested_root: Path | None
    scopes: tuple[LibraryScope, ...]
    media: tuple[CheckedMedia, ...]
    findings: tuple[LibraryFinding, ...]

    @property
    def errors(self) -> int:
        return sum(item.severity == "error" for item in self.findings)

    @property
    def warnings(self) -> int:
        return sum(item.severity == "warning" for item in self.findings)

    @property
    def infos(self) -> int:
        return sum(item.severity == "info" for item in self.findings)

    @property
    def kavita_ready(self) -> bool:
        return self.errors == 0

    @property
    def canonical(self) -> bool:
        return self.errors == 0 and self.warnings == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "requested_root": str(self.requested_root) if self.requested_root else None,
            "scopes": [scope.to_dict() for scope in self.scopes],
            "summary": {
                "media_files": len(self.media),
                "errors": self.errors,
                "warnings": self.warnings,
                "info": self.infos,
                "kavita_ready": self.kavita_ready,
                "canonical": self.canonical,
            },
            "media": [item.to_dict() for item in self.media],
            "findings": [item.to_dict() for item in self.findings],
        }


def check_library(root: Path | None, config: AppConfig) -> LibraryCheckResult:
    """Read-only audit of configured Kavita library roots and kavita-ingest layout drift.

    The checker intentionally performs no provider calls, database writes, metadata writes,
    renames, or source lifecycle operations. It diagnoses two related contracts:

    * Kavita readiness: locally inspectable media is not placed directly at the library root
      and a series folder does not mix conflicting embedded series identities.
    * kavita-ingest canonical layout: paths, containers, and embedded naming metadata agree
      with the configured naming policy used by the publisher.
    """

    scopes = _resolve_scopes(root, config)
    findings: list[LibraryFinding] = []
    checked: list[CheckedMedia] = []
    series_folders: dict[tuple[Path, str], set[str]] = defaultdict(set)
    series_locations: dict[tuple[Path, str], set[str]] = defaultdict(set)
    comic_slots: dict[tuple[Path, str, str, str, str], list[Path]] = defaultdict(list)

    for scope in scopes:
        scope_findings, scope_media = _check_scope(
            scope,
            config,
            series_folders=series_folders,
            series_locations=series_locations,
            comic_slots=comic_slots,
        )
        findings.extend(scope_findings)
        checked.extend(scope_media)

    findings.extend(_cross_file_findings(series_folders, series_locations, comic_slots))
    findings.sort(key=_finding_sort_key)
    finding_paths = {item.path for item in findings if item.severity in {"error", "warning"}}
    checked = [
        CheckedMedia(item.path, item.kind, item.path not in finding_paths) for item in checked
    ]
    checked.sort(key=lambda item: str(item.path).casefold())
    return LibraryCheckResult(root, scopes, tuple(checked), tuple(findings))


def _resolve_scopes(root: Path | None, config: AppConfig) -> tuple[LibraryScope, ...]:
    configured: list[tuple[LibraryKind, Path]] = []
    if config.books_root is not None:
        configured.append(("books", config.books_root.expanduser().resolve(strict=False)))
    if config.comics_root is not None:
        configured.append(("comics", config.comics_root.expanduser().resolve(strict=False)))
    if not configured:
        raise ValueError("library-check requires configured books and/or comics destination roots")

    if root is None:
        scopes = [
            LibraryScope(kind, library_root, library_root)
            for kind, library_root in configured
            if library_root.exists()
        ]
    else:
        requested = root.expanduser().resolve(strict=True)
        if not requested.is_dir():
            raise ValueError(f"library-check root is not a directory: {requested}")
        scopes = []
        for kind, library_root in configured:
            if _contains(requested, library_root):
                if library_root.exists():
                    scopes.append(LibraryScope(kind, library_root, library_root))
            elif _contains(library_root, requested):
                scopes.append(LibraryScope(kind, library_root, requested))

    if not scopes:
        configured_text = ", ".join(str(path) for _, path in configured)
        raise ValueError(
            "requested root does not contain, or sit within, a configured Kavita library root; "
            f"configured roots: {configured_text}"
        )
    return tuple(scopes)


def _check_scope(
    scope: LibraryScope,
    config: AppConfig,
    *,
    series_folders: dict[tuple[Path, str], set[str]],
    series_locations: dict[tuple[Path, str], set[str]],
    comic_slots: dict[tuple[Path, str, str, str, str], list[Path]],
) -> tuple[list[LibraryFinding], list[CheckedMedia]]:
    findings: list[LibraryFinding] = []
    checked: list[CheckedMedia] = []
    limits = config.archive_limits()

    for current, directories, files in os.walk(scope.scan_root, followlinks=False):
        current_path = Path(current)
        symlink_dirs = [name for name in directories if (current_path / name).is_symlink()]
        for name in symlink_dirs:
            path = current_path / name
            findings.append(
                LibraryFinding(
                    "warning",
                    "symlink_directory_skipped",
                    path,
                    "symlinked directories are not audited by kavita-ingest",
                )
            )
        directories[:] = sorted(name for name in directories if name not in symlink_dirs)

        for name in sorted(files, key=str.casefold):
            path = current_path / name
            if path.is_symlink():
                findings.append(
                    LibraryFinding(
                        "warning",
                        "symlink_file_skipped",
                        path,
                        "symlinked media is outside kavita-ingest's publication contract",
                    )
                )
                continue
            if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
                continue

            local_findings = _check_media_file(
                path,
                scope,
                config,
                limits,
                series_folders=series_folders,
                series_locations=series_locations,
                comic_slots=comic_slots,
            )
            findings.extend(local_findings)
            canonical = not any(
                item.severity in {"error", "warning"} and item.path == path
                for item in local_findings
            )
            checked.append(CheckedMedia(path, scope.kind, canonical))

    return findings, checked


def _check_media_file(
    path: Path,
    scope: LibraryScope,
    config: AppConfig,
    limits: ArchiveLimits,
    *,
    series_folders: dict[tuple[Path, str], set[str]],
    series_locations: dict[tuple[Path, str], set[str]],
    comic_slots: dict[tuple[Path, str, str, str, str], list[Path]],
) -> list[LibraryFinding]:
    findings: list[LibraryFinding] = []
    relative = path.relative_to(scope.library_root)
    if len(relative.parts) == 1:
        findings.append(
            LibraryFinding(
                "error",
                "media_at_library_root",
                path,
                (
                    "Kavita media must be stored below a series/book folder, "
                    "not directly at the library root"
                ),
            )
        )

    try:
        signature, detected = detect_signature(path)
    except OSError as exc:
        return [
            *findings,
            LibraryFinding(
                "error", "unreadable_media", path, f"cannot read media signature: {exc}"
            ),
        ]

    expected_format = _format_for_suffix(path.suffix.casefold())
    if detected is SourceFormat.UNKNOWN:
        return [
            *findings,
            LibraryFinding(
                "error",
                "unsupported_signature",
                path,
                f"supported filename extension has an unrecognised content signature ({signature})",
            ),
        ]
    if expected_format is not None and detected is not expected_format:
        findings.append(
            LibraryFinding(
                "error",
                "extension_signature_mismatch",
                path,
                (
                    f"filename extension indicates {expected_format.value}, "
                    f"but content is {detected.value}"
                ),
                expected=f".{detected.value}",
            )
        )

    inspection = inspect(path, detected, limits)
    if inspection.status is not InspectionStatus.OK:
        findings.append(
            LibraryFinding(
                "error",
                inspection.error_code or "inspection_failed",
                path,
                inspection.error_message or "media inspection failed",
            )
        )
        return findings

    classification = classify(path, detected, inspection)
    expected_kind = MediaKind.BOOK if scope.kind == "books" else MediaKind.COMIC
    if detected is not SourceFormat.PDF and classification.kind is not expected_kind:
        findings.append(
            LibraryFinding(
                "error",
                "library_kind_mismatch",
                path,
                (
                    f"file is classified as {classification.kind.value}, "
                    f"but is stored in the {scope.kind} library"
                ),
            )
        )

    if scope.kind == "books":
        findings.extend(_check_book(path, scope, config, inspection, classification))
    else:
        findings.extend(
            _check_comic(
                path,
                scope,
                config,
                inspection,
                classification,
                detected,
                series_folders=series_folders,
                series_locations=series_locations,
                comic_slots=comic_slots,
            )
        )
    return findings


def _check_book(
    path: Path,
    scope: LibraryScope,
    config: AppConfig,
    inspection: InspectionResult,
    classification: Classification,
) -> list[LibraryFinding]:
    findings: list[LibraryFinding] = []
    policy = config.naming_policy()
    metadata = inspection.metadata
    hypothesis = classification.hypotheses[0]

    title = str(metadata.get("title") or "").strip()
    creators_raw = metadata.get("creators")
    creators = (
        tuple(str(item).strip() for item in creators_raw if str(item).strip())
        if isinstance(creators_raw, list)
        else ()
    )
    if not title and inspection.format is SourceFormat.PDF:
        info = metadata.get("document_info")
        if isinstance(info, dict):
            title = str(info.get("title") or "").strip()
            author = str(info.get("author") or "").strip()
            creators = (author,) if author else ()
    if not title:
        findings.append(
            LibraryFinding(
                "warning",
                "book_title_metadata_missing",
                path,
                "book title metadata is missing; Kavita will have to fall back to filename parsing",
            )
        )
        title = hypothesis.title or path.stem
    if not creators:
        findings.append(
            LibraryFinding(
                "warning",
                "book_creator_metadata_missing",
                path,
                "book creator/author metadata is missing",
            )
        )

    series = str(metadata.get("series") or hypothesis.series or "").strip() or None
    sequence: SequenceNumber | None = None
    series_index = metadata.get("series_index")
    if isinstance(series_index, str) and series_index.strip():
        try:
            sequence = SequenceNumber.parse(series_index)
        except ValueError:
            findings.append(
                LibraryFinding(
                    "warning",
                    "invalid_book_series_index",
                    path,
                    f"series index cannot be parsed: {series_index!r}",
                )
            )
    elif hypothesis.sequence is not None:
        sequence = hypothesis.sequence

    values = {
        "title": title,
        "series": series,
        "series_or_title": series or title,
        "number": render_sequence(sequence, policy.integer_padding),
        "year": hypothesis.year,
        "author": creators[0] if creators else None,
        "format": None,
    }
    folder = PurePosixPath(render_component(policy.book_folder, values))
    template = policy.book_series_file if series else policy.book_file
    filename = render_component(template, values) + path.suffix.casefold()
    expected_relative = folder / filename
    actual_relative = PurePosixPath(path.relative_to(scope.library_root).as_posix())
    if actual_relative != expected_relative:
        findings.append(
            LibraryFinding(
                "warning",
                "noncanonical_book_path",
                path,
                "book path does not match the configured kavita-ingest naming policy",
                expected=expected_relative.as_posix(),
            )
        )
    return findings


def _check_comic(
    path: Path,
    scope: LibraryScope,
    config: AppConfig,
    inspection: InspectionResult,
    classification: Classification,
    detected: SourceFormat,
    *,
    series_folders: dict[tuple[Path, str], set[str]],
    series_locations: dict[tuple[Path, str], set[str]],
    comic_slots: dict[tuple[Path, str, str, str, str], list[Path]],
) -> list[LibraryFinding]:
    findings: list[LibraryFinding] = []
    policy = config.naming_policy()
    if detected is SourceFormat.PDF:
        return _check_comic_pdf_path(path, scope)

    comicinfo = inspection.metadata.get("comicinfo")

    if detected in {SourceFormat.CBZ, SourceFormat.CBR} and not isinstance(comicinfo, dict):
        findings.append(
            LibraryFinding(
                "warning",
                "comicinfo_missing",
                path,
                "comic archive has no ComicInfo.xml; Kavita must rely on filename/folder parsing",
            )
        )
        comicinfo = {}
    if not isinstance(comicinfo, dict):
        comicinfo = {}

    hypothesis = classification.hypotheses[0]
    series = str(comicinfo.get("Series") or hypothesis.series or "").strip()
    title = str(comicinfo.get("Title") or hypothesis.title or "").strip()
    format_value = str(comicinfo.get("Format") or "").strip()
    number_text = str(comicinfo.get("Number") or "").strip()
    volume_text = str(comicinfo.get("Volume") or "").strip()

    if title and _looks_like_bare_edition_label(title):
        findings.append(
            LibraryFinding(
                "warning",
                "comic_title_is_only_edition_label",
                path,
                (
                    f"ComicInfo Title {title!r} looks like an edition qualifier rather "
                    "than a descriptive item title"
                ),
            )
        )

    if volume_text and not format_value:
        findings.append(
            LibraryFinding(
                "warning",
                "collection_volume_without_format",
                path,
                "ComicInfo Volume is set but Format is empty; collection semantics are incomplete",
            )
        )

    if not series:
        findings.append(
            LibraryFinding(
                "error",
                "comic_series_unresolved",
                path,
                "comic series cannot be resolved from ComicInfo or local filename evidence",
            )
        )
        return findings

    if detected is SourceFormat.CBR:
        findings.append(
            LibraryFinding(
                "warning",
                "noncanonical_cbr_container",
                path,
                (
                    "Kavita can read CBR/RAR, but kavita-ingest canonical publication "
                    "converts comic archives to CBZ"
                ),
                expected=path.with_suffix(".cbz").name,
            )
        )

    top_folder = _top_folder(path, scope.library_root)
    normalized_series = series.casefold()
    series_folders[(scope.library_root, top_folder)].add(normalized_series)
    series_locations[(scope.library_root, normalized_series)].add(top_folder)

    series_year = _trailing_series_year(series)

    special_formats = {
        "annual",
        "special",
        "one-shot",
        "trade paperback",
        "omnibus",
        "graphic novel",
    }
    special = format_value.casefold() in special_formats
    if format_value and not special:
        findings.append(
            LibraryFinding(
                "warning",
                "noncanonical_comic_format",
                path,
                f"ComicInfo Format is outside kavita-ingest's supported set: {format_value!r}",
            )
        )
    expected_parent = PurePosixPath(
        render_component(
            policy.comic_folder,
            {
                "title": title or None,
                "series": series,
                "series_or_title": series,
                "number": None,
                "year": series_year,
                "author": None,
                "format": format_value or None,
            },
        )
    )
    if special and policy.comic_specials_subfolder:
        expected_parent /= "Specials"

    number_rendered: str | None = None
    slot_type = "item"
    slot_value = ""
    collection_format = format_value.casefold() in {"trade paperback", "omnibus"}
    inferred_volume: int | None = None
    if collection_format and number_text and not volume_text:
        findings.append(
            LibraryFinding(
                "warning",
                "collection_format_uses_issue_number",
                path,
                (
                    "collection-like ComicInfo stores its index in Number; current "
                    "kavita-ingest publications use integer Volume and clear Number"
                ),
            )
        )
        if number_text.isdigit():
            inferred_volume = int(number_text)

    if volume_text:
        try:
            volume = int(volume_text)
        except ValueError:
            findings.append(
                LibraryFinding(
                    "warning",
                    "noninteger_collection_volume",
                    path,
                    f"ComicInfo Volume is not an integer collection volume: {volume_text!r}",
                )
            )
            number_rendered = volume_text
        else:
            number_rendered = f"v{volume:02d}"
            slot_type, slot_value = "volume", str(volume)
        if number_text:
            findings.append(
                LibraryFinding(
                    "warning",
                    "comic_number_and_volume_set",
                    path,
                    (
                        "ComicInfo sets both Number and Volume; kavita-ingest collection "
                        "publications keep Number empty"
                    ),
                )
            )
    elif inferred_volume is not None:
        number_rendered = f"v{inferred_volume:02d}"
        slot_type, slot_value = "volume", str(inferred_volume)
    elif number_text:
        try:
            sequence = SequenceNumber.parse(number_text)
            number_rendered = render_sequence(sequence, policy.integer_padding)
            slot_type, slot_value = "number", sequence.normalized
        except ValueError:
            findings.append(
                LibraryFinding(
                    "warning",
                    "invalid_comic_number",
                    path,
                    f"ComicInfo Number cannot be parsed: {number_text!r}",
                )
            )
            number_rendered = number_text
    elif format_value.casefold() == "one-shot":
        number_rendered = render_sequence(SequenceNumber.parse("1"), policy.integer_padding)
        slot_type, slot_value = "number", "1"
    elif not special:
        findings.append(
            LibraryFinding(
                "warning",
                "comic_sequence_missing",
                path,
                "ordinary comic has no Number metadata and will rely on filename parsing",
            )
        )

    values = {
        "title": title or None,
        "series": series,
        "series_or_title": series,
        "number": number_rendered,
        "year": series_year,
        "author": None,
        "format": format_value or None,
    }
    expected_extension = ".pdf" if detected is SourceFormat.PDF else ".cbz"
    expected_filename = render_component(policy.comic_file, values) + expected_extension
    expected_relative = expected_parent / expected_filename
    actual_relative = PurePosixPath(path.relative_to(scope.library_root).as_posix())

    expected_top = expected_parent.parts[0]
    if top_folder != expected_top:
        findings.append(
            LibraryFinding(
                "error",
                "comic_series_folder_mismatch",
                path,
                "top-level comic folder does not agree with the embedded/local series identity",
                expected=expected_top,
            )
        )
    elif actual_relative.parent != expected_parent:
        findings.append(
            LibraryFinding(
                "warning",
                "noncanonical_comic_folder",
                path,
                "comic subfolder does not match the configured kavita-ingest layout",
                expected=expected_parent.as_posix(),
            )
        )
    if actual_relative != expected_relative:
        findings.append(
            LibraryFinding(
                "warning",
                "noncanonical_comic_path",
                path,
                "comic filename/path does not match the configured kavita-ingest naming policy",
                expected=expected_relative.as_posix(),
            )
        )

    if slot_value:
        comic_slots[
            (scope.library_root, normalized_series, format_value.casefold(), slot_type, slot_value)
        ].append(path)
    return findings


def _check_comic_pdf_path(path: Path, scope: LibraryScope) -> list[LibraryFinding]:
    findings: list[LibraryFinding] = []
    relative = path.relative_to(scope.library_root)
    if len(relative.parts) < 2:
        return findings
    top_folder = relative.parts[0]
    parent = PurePosixPath(relative.parent.as_posix())
    if parent.parts[-1].casefold() == "specials":
        expected_series_folder = parent.parts[-2] if len(parent.parts) >= 2 else ""
    else:
        expected_series_folder = top_folder
    if expected_series_folder and not path.stem.casefold().startswith(
        expected_series_folder.casefold()
    ):
        findings.append(
            LibraryFinding(
                "warning",
                "comic_pdf_series_prefix_mismatch",
                path,
                (
                    "comic PDF filename does not begin with its series folder; PDF metadata "
                    "is not rich enough for a stronger canonical-path reconstruction"
                ),
                expected=f"{expected_series_folder} - ...{path.suffix.casefold()}",
            )
        )
    if len(relative.parts) > 3 or (len(relative.parts) == 3 and relative.parts[-2] != "Specials"):
        findings.append(
            LibraryFinding(
                "warning",
                "noncanonical_comic_pdf_nesting",
                path,
                "comic PDF is nested more deeply than the configured series[/Specials]/file layout",
            )
        )
    return findings


def _cross_file_findings(
    series_folders: dict[tuple[Path, str], set[str]],
    series_locations: dict[tuple[Path, str], set[str]],
    comic_slots: dict[tuple[Path, str, str, str, str], list[Path]],
) -> list[LibraryFinding]:
    findings: list[LibraryFinding] = []
    for (library_root, folder), series_values in series_folders.items():
        if len(series_values) > 1:
            findings.append(
                LibraryFinding(
                    "error",
                    "mixed_series_folder",
                    library_root / folder,
                    "one top-level comic folder contains multiple embedded series identities",
                )
            )
    for (library_root, series), folders in series_locations.items():
        if len(folders) > 1:
            findings.append(
                LibraryFinding(
                    "warning",
                    "series_split_across_folders",
                    library_root,
                    (
                        f"series {series!r} is split across top-level folders: "
                        f"{', '.join(sorted(folders))}"
                    ),
                )
            )
    for (_root, series, format_value, slot_type, slot_value), paths in comic_slots.items():
        if len(paths) > 1:
            for path in paths:
                findings.append(
                    LibraryFinding(
                        "error",
                        "duplicate_comic_slot",
                        path,
                        (
                            f"multiple files claim {series!r} "
                            f"{format_value or 'standard'} {slot_type} {slot_value}"
                        ),
                    )
                )
    return findings


def _looks_like_bare_edition_label(value: str) -> bool:
    cleaned = re.sub(r"\s+", " ", value.strip())
    return bool(
        re.fullmatch(
            r"(?:\d+(?:st|nd|rd|th) edition|(?:dc )?black label edition|"
            r"deluxe edition|essential edition|absolute edition|collected edition|"
            r"trade paperback|hardcover|omnibus|compendium)",
            cleaned,
            re.IGNORECASE,
        )
    )


def _trailing_series_year(series: str) -> int | None:
    match = re.search(r"\(((?:19|20)\d{2})\)$", series.strip())
    return int(match.group(1)) if match else None


def _format_for_suffix(suffix: str) -> SourceFormat | None:
    return {
        ".epub": SourceFormat.EPUB,
        ".pdf": SourceFormat.PDF,
        ".cbz": SourceFormat.CBZ,
        ".cbr": SourceFormat.CBR,
        ".rar": SourceFormat.CBR,
    }.get(suffix)


def _top_folder(path: Path, library_root: Path) -> str:
    relative = path.relative_to(library_root)
    return relative.parts[0] if len(relative.parts) > 1 else ""


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


def _finding_sort_key(item: LibraryFinding) -> tuple[int, str, str]:
    severity = {"error": 0, "warning": 1, "info": 2}[item.severity]
    return severity, str(item.path).casefold(), item.code
