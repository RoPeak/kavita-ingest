from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .domain import (
    Classification,
    Evidence,
    InspectionResult,
    MediaKind,
    ParseHypothesis,
    SequenceNumber,
    SourceFormat,
)

_YEAR = re.compile(r"\((19\d{2}|20\d{2})\)")
_NOISE_TERMS = re.compile(
    r"^(digital|webrip|c2c|\d+ covers?|empire|dcp|pyrate(?:gonekiwi)?|"
    r"[^()]*-(?:empire|dcp))$",
    re.IGNORECASE,
)
_TRAILING_RELEASE_GROUP = re.compile(r"\s*\(([^()]*(?:Empire|DCP))\)\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FilenameToken:
    raw: str
    normalized: str
    kind: str
    span: tuple[int, int]
    is_noise: bool = False


def tokenize_filename(path: Path) -> tuple[FilenameToken, ...]:
    text = path.stem
    tokens: list[FilenameToken] = []
    occupied: list[tuple[int, int]] = []
    for match in re.finditer(r"\(([^()]*)\)", text):
        raw = match.group(1).strip()
        kind = "year" if re.fullmatch(r"(?:19|20)\d{2}", raw) else "parenthetical"
        noise = bool(_NOISE_TERMS.fullmatch(raw))
        tokens.append(
            FilenameToken(
                raw, _normalize(raw), "release-noise" if noise else kind, match.span(), noise
            )
        )
        occupied.append(match.span())
    for match in re.finditer(r"#?\b(?:\d+(?:\.\d+)?[A-Za-z]?|TPB\d+)\b", text, re.IGNORECASE):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        raw = match.group(0).lstrip("#")
        tokens.append(FilenameToken(raw, _normalize(raw), "sequence", match.span()))
    remainder = re.sub(r"\([^()]*\)", " ", text)
    for match in re.finditer(r"[^\s_-]+", remainder):
        tokens.append(
            FilenameToken(match.group(0), _normalize(match.group(0)), "text", match.span())
        )
    return tuple(sorted(tokens, key=lambda token: (token.span[0], token.span[1], token.kind)))


def classify(path: Path, format_: SourceFormat, inspection: InspectionResult) -> Classification:
    evidence = list(_filename_evidence(path))
    evidence.extend(inspection.evidence)
    if format_ is SourceFormat.EPUB:
        hypothesis = _book_hypothesis(path, inspection, tuple(evidence), 0.98)
        return Classification(
            MediaKind.BOOK, hypothesis.subtype, hypothesis.confidence, False, (hypothesis,)
        )
    if format_ in {SourceFormat.CBZ, SourceFormat.CBR}:
        hypothesis = _comic_hypothesis(path, inspection, tuple(evidence), 0.98)
        return Classification(
            MediaKind.COMIC, hypothesis.subtype, hypothesis.confidence, False, (hypothesis,)
        )
    if format_ is SourceFormat.PDF:
        return _classify_pdf(path, inspection, tuple(evidence))
    unknown = ParseHypothesis(
        MediaKind.UNKNOWN,
        "unknown",
        0.0,
        title=_clean_title(path.stem),
        evidence=tuple(evidence),
        reasons=("unsupported or mismatched file signature",),
    )
    return Classification(MediaKind.UNKNOWN, "unknown", 0.0, True, (unknown,))


def _classify_pdf(
    path: Path, inspection: InspectionResult, evidence: tuple[Evidence, ...]
) -> Classification:
    text = f"{path.parent.name} {path.stem}"
    comic_signal = bool(
        re.search(r"#\s*\d+", text)
        or re.search(r"\bV\d+\s*-\s*\d+\b", text, re.IGNORECASE)
        or re.search(r"\b(?:issue|comic|watchmen|doomsday clock)\b", text, re.IGNORECASE)
    )
    book_signal = bool(
        re.search(r"\b(?:handbook|manual|development|guide|novel)\b", text, re.IGNORECASE)
        or inspection.metadata.get("document_info", {}).get("author")
    )
    comic_score = 0.92 if comic_signal else (0.32 if book_signal else 0.48)
    book_score = 0.92 if book_signal and not comic_signal else (0.28 if comic_signal else 0.52)
    comic = _comic_hypothesis(path, inspection, evidence, comic_score)
    book = _book_hypothesis(path, inspection, evidence, book_score)
    ordered = tuple(sorted((comic, book), key=lambda item: item.confidence, reverse=True))
    winner = ordered[0]
    ambiguous = (
        winner.confidence < 0.75 or abs(ordered[0].confidence - ordered[1].confidence) < 0.20
    )
    return Classification(winner.kind, winner.subtype, winner.confidence, ambiguous, ordered)


def _book_hypothesis(
    path: Path,
    inspection: InspectionResult,
    evidence: tuple[Evidence, ...],
    confidence: float,
) -> ParseHypothesis:
    stem = _without_release_noise(path.stem)
    year_match = _YEAR.search(path.stem)
    year = int(year_match.group(1)) if year_match else _trailing_year(stem)
    stem = _YEAR.sub("", stem).strip(" -_")
    stem = re.sub(r"\s+(?:19|20)\d{2}$", "", stem).strip()
    creators: tuple[str, ...] = ()
    if match := re.match(r"^(.*?)\s+by\s+(.+)$", stem, re.IGNORECASE):
        stem, creator = match.groups()
        creators = (creator.strip(),)
    elif re.search(r"\bHandbook\s+[A-Z][a-z]+\s+[A-Z][a-z]+$", stem):
        match = re.match(r"^(.*?Handbook)\s+([A-Z][a-z]+\s+[A-Z][a-z]+)$", stem)
        if match:
            stem, creator = match.groups()
            creators = (creator,)
    opf_creators = inspection.metadata.get("creators", [])
    if isinstance(opf_creators, list) and opf_creators:
        creators = tuple(str(item) for item in opf_creators)
    title = str(inspection.metadata.get("title") or _clean_title(stem))
    sequence = None
    series = inspection.metadata.get("series")
    series_index = inspection.metadata.get("series_index")
    if isinstance(series_index, str) and series_index:
        sequence = SequenceNumber.parse(series_index)
    return ParseHypothesis(
        MediaKind.BOOK,
        "series-book" if series else "standalone-book",
        confidence,
        title=title,
        series=str(series) if series else None,
        sequence=sequence,
        year=year,
        creators=creators,
        evidence=evidence,
        reasons=(
            "EPUB container"
            if path.suffix.casefold() == ".epub"
            else "book-like filename evidence",
        ),
    )


def _comic_hypothesis(
    path: Path,
    inspection: InspectionResult,
    evidence: tuple[Evidence, ...],
    confidence: float,
) -> ParseHypothesis:
    stem = _without_release_noise(path.stem)
    year_match = _YEAR.search(stem)
    year = int(year_match.group(1)) if year_match else None
    stem = _YEAR.sub("", stem).strip(" -_")
    comicinfo = inspection.metadata.get("comicinfo", {})
    if not isinstance(comicinfo, dict):
        comicinfo = {}
    title = str(comicinfo.get("Title")) if comicinfo.get("Title") else None
    series = str(comicinfo.get("Series")) if comicinfo.get("Series") else None
    sequence = SequenceNumber.parse(str(comicinfo["Number"])) if comicinfo.get("Number") else None
    creators: tuple[str, ...] = ()
    subtype = "issue"

    collection = re.match(
        r"^(.*?)\s+by\s+(.+?)\s+(?:(Ultimate Collection)\s+)?Book\s+([\w.-]+)$",
        stem,
        re.IGNORECASE,
    )
    if collection:
        series_text, creator, collection_label, number = collection.groups()
        series = series or _clean_title(series_text)
        title = title or (
            f"{collection_label} Book {number}" if collection_label else f"Book {number}"
        )
        creators = (_clean_title(creator),)
        sequence = sequence or SequenceNumber.parse(number)
        subtype = "collected-edition"
    elif re.search(
        r"\b(?:TPB|Omnibus|Ultimate Collection|Collected Edition)\b", stem, re.IGNORECASE
    ):
        subtype = "collected-edition"
        match = re.search(r"\b(?:Book|TPB|Volume|Vol\.?)\s*([\w.-]+)", stem, re.IGNORECASE)
        if match:
            sequence = sequence or SequenceNumber.parse(match.group(1))
        series = series or _clean_title(
            re.split(r"\b(?:TPB|Omnibus|Ultimate Collection)\b", stem, flags=re.IGNORECASE)[0]
        )
        title = title or _clean_title(stem)
    else:
        patterns = (
            re.match(
                r"^(.+?)\s*-\s*V\d+\s*-\s*([\w.]+)\s*-\s*(.+)$",
                stem,
                re.IGNORECASE,
            ),
            re.match(r"^(.*?)\s+#([\w.-]+)$", stem),
            re.match(r"^(.*?)\s+([0-9]+(?:\.[0-9]+)?[A-Za-z]?)\s*(?:\([^)]*\))?$", stem),
        )
        match = next((item for item in patterns if item), None)
        if match:
            groups = match.groups()
            series = series or _clean_title(groups[0])
            sequence = sequence or SequenceNumber.parse(groups[1])
            if len(groups) > 2:
                title = title or _clean_title(groups[2])
        elif re.search(r"\b(?:Annual|Special)\b", stem, re.IGNORECASE):
            subtype = "annual" if re.search(r"\bAnnual\b", stem, re.IGNORECASE) else "special"
            series = series or _clean_title(stem)
        else:
            subtype = "one-shot"
            series = series or _clean_title(stem)
            title = title or _clean_title(stem)
    if not series and path.parent.name and path.parent.name not in {".", path.anchor}:
        series = _clean_title(path.parent.name)
    return ParseHypothesis(
        MediaKind.COMIC,
        subtype,
        confidence,
        title=title,
        series=series,
        sequence=sequence,
        year=year,
        creators=creators,
        evidence=evidence,
        reasons=("comic archive or strong issue/folder evidence",),
    )


def _filename_evidence(path: Path) -> tuple[Evidence, ...]:
    output = [
        Evidence("filename", path.name, path.stem, "filename", 1.0),
        Evidence("folder", path.parent.name, path.parent.name, "parent-folder", 0.75),
    ]
    output.extend(
        Evidence(
            token.kind,
            token.raw,
            token.normalized,
            "filename-token",
            0.8,
            token.is_noise,
            token.span,
        )
        for token in tokenize_filename(path)
    )
    return tuple(output)


def _without_release_noise(text: str) -> str:
    text = _TRAILING_RELEASE_GROUP.sub("", text)

    def replace(match: re.Match[str]) -> str:
        return "" if _NOISE_TERMS.fullmatch(match.group(1).strip()) else match.group(0)

    return re.sub(r"\(([^()]*)\)", replace, text).strip(" -_")


def _clean_title(value: str) -> str:
    value = value.replace("_", " ")
    value = re.sub(r"\s+-\s+", " - ", value)
    return re.sub(r"\s+", " ", value).strip(" -_")


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _trailing_year(value: str) -> int | None:
    match = re.search(r"\b((?:19|20)\d{2})$", value)
    return int(match.group(1)) if match else None
