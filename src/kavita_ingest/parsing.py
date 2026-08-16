from __future__ import annotations

import re
from dataclasses import dataclass, replace
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
    r"^(digital|digital[-_ ]?tpb|webrip|c2c|\d+ covers?|empire|dcp|pyrate(?:gonekiwi)?|"
    r"[^()]*-(?:empire|dcp))$",
    re.IGNORECASE,
)
_TRAILING_RELEASE_GROUP = re.compile(r"\s*\(([^()]*(?:Empire|DCP))\)\s*$", re.IGNORECASE)
_TRAILING_SOURCE_SITE = re.compile(
    r"\s+(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*"
    r"\.(?:com|net|org|info|io|co|me|cc)\s*$",
    re.IGNORECASE,
)
_COMPOUND_YEAR = re.compile(r"^((?:19|20)\d{2})\s*,\s*(.+)$", re.IGNORECASE)
_EDITION_QUALIFIER = re.compile(
    r"(?:\bedition\b|\bcollection\b|\bomnibus\b|\bcompendium\b|"
    r"\babsolute\b|\bcompact\s+comics\b|\bhardcover\b|"
    r"\btrade\s+paperback\b|\btpb\b|\bin\s+one\s+volume\b)",
    re.IGNORECASE,
)
_COLLECTION_VOLUME_SHORTHAND = re.compile(r"^(.*?)\s+[vV](\d{1,4})$")
_COMPLETE_ONE_VOLUME_SUFFIX = re.compile(
    r"^(.*?)\s+-\s+((?:The\s+)?Complete\b.+\bin\s+One\s+Volume)$",
    re.IGNORECASE,
)


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
        noise = bool(_NOISE_TERMS.fullmatch(raw))
        compound = _compound_year_qualifier(raw)
        if compound is not None:
            year, qualifier = compound
            tokens.append(FilenameToken(str(year), str(year), "year", match.span()))
            tokens.append(
                FilenameToken(
                    qualifier,
                    _normalize(qualifier),
                    "edition-qualifier",
                    match.span(),
                )
            )
        else:
            kind = "year" if re.fullmatch(r"(?:19|20)\d{2}", raw) else "parenthetical"
            if _looks_like_edition_qualifier(raw) and not noise:
                kind = "edition-qualifier"
            tokens.append(
                FilenameToken(
                    raw,
                    _normalize(raw),
                    "release-noise" if noise else kind,
                    match.span(),
                    noise,
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
        raw = match.group(0)
        source_site = bool(_looks_like_source_site(raw))
        tokens.append(
            FilenameToken(
                raw,
                _normalize(raw),
                "release-noise" if source_site else "text",
                match.span(),
                source_site,
            )
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
        page_count = inspection.metadata.get("page_count")
        if (
            hypothesis.subtype == "one-shot"
            and isinstance(page_count, int)
            and page_count >= 120
        ):
            credited_series, credited_creators = _split_creator_credit(
                hypothesis.series or hypothesis.title or ""
            )
            collected = replace(
                hypothesis,
                subtype="collected-edition",
                confidence=0.78,
                title=credited_series or hypothesis.title,
                series=credited_series or hypothesis.series,
                creators=credited_creators or hypothesis.creators,
                reasons=(
                    *hypothesis.reasons,
                    "long unnumbered archive may collect multiple issues",
                    *(
                        ("explicit filename creator credit retained as collection evidence",)
                        if credited_creators
                        else ()
                    ),
                ),
            )
            one_shot = replace(
                hypothesis,
                confidence=0.72,
                reasons=(*hypothesis.reasons, "a long original graphic work remains possible"),
            )
            return Classification(
                MediaKind.COMIC,
                collected.subtype,
                collected.confidence,
                True,
                (collected, one_shot),
            )
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
    stem, year, edition_qualifiers = _comic_filename_semantics(path.stem)
    comicinfo = inspection.metadata.get("comicinfo", {})
    if not isinstance(comicinfo, dict):
        comicinfo = {}
    title = str(comicinfo.get("Title")) if comicinfo.get("Title") else None
    series = str(comicinfo.get("Series")) if comicinfo.get("Series") else None
    sequence = SequenceNumber.parse(str(comicinfo["Number"])) if comicinfo.get("Number") else None
    creators: tuple[str, ...] = ()
    subtype = "issue"
    identity_reasons: list[str] = []

    collection = re.match(
        r"^(.*?)\s+by\s+(.+?)\s+(?:(Ultimate Collection)\s+)?Book\s+([\w.-]+)$",
        stem,
        re.IGNORECASE,
    )
    if collection:
        series_text, creator, collection_label, number = collection.groups()
        series = _clean_title(series_text)
        title = title or (
            f"{collection_label} Book {number}" if collection_label else f"Book {number}"
        )
        creators = (_clean_title(creator),)
        sequence = sequence or SequenceNumber.parse(number)
        subtype = "collected-edition"
        if collection_label:
            edition_qualifiers = _merge_qualifiers(edition_qualifiers, collection_label)
        if comicinfo.get("Series") and _normalize(str(comicinfo["Series"])) != _normalize(series):
            identity_reasons.append(
                "structured collection filename separates series, creator, and book index; "
                "embedded ComicInfo Series is retained as conflicting edition-label evidence"
            )
    elif re.search(
        r"\b(?:TPB|Omnibus|Ultimate Collection|Collected Edition)\b"
        r"|\b(?:Volume|Vol\.?)\s*(?:\d+(?:\.\d+)?|[IVXLCDM]+)\b",
        stem,
        re.IGNORECASE,
    ):
        subtype = "collected-edition"
        match = re.search(
            r"\b(?:Book|TPB|Volume|Vol\.?)\s*([\w.-]+)", stem, re.IGNORECASE
        )
        if match:
            sequence = sequence or SequenceNumber.parse(match.group(1))
            filename_series = _clean_title(stem[: match.start()].rstrip(" ,-"))
            if filename_series:
                series = filename_series
            title = (
                f"Volume {int(match.group(1))}"
                if match.group(1).isdigit()
                else _clean_title(stem)
            )
        else:
            filename_series = _clean_title(
                re.split(
                    r"\b(?:TPB|Omnibus|Ultimate Collection|Collected Edition)\b",
                    stem,
                    flags=re.IGNORECASE,
                )[0]
            )
            if filename_series:
                series = filename_series
            title = _clean_title(stem)
        qualifier = _first_edition_qualifier(stem)
        if qualifier:
            edition_qualifiers = _merge_qualifiers(edition_qualifiers, qualifier)
    else:
        volume = _COLLECTION_VOLUME_SHORTHAND.match(stem)
        one_volume = _COMPLETE_ONE_VOLUME_SUFFIX.match(stem)
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
        if volume:
            series_text, number = volume.groups()
            series = _clean_title(series_text)
            sequence = sequence or SequenceNumber.parse(number)
            title = f"Volume {int(number)}"
            subtype = "collected-edition"
        elif one_volume:
            series_text, qualifier = one_volume.groups()
            series = _clean_title(series_text)
            title = _clean_title(qualifier)
            edition_qualifiers = _merge_qualifiers(edition_qualifiers, qualifier)
            subtype = "collected-edition"
        elif match:
            groups = match.groups()
            filename_series = _clean_title(groups[0])
            if series and _series_creator_alias_base(series) == _normalize(filename_series):
                identity_reasons.append(
                    "filename issue syntax removes a trailing creator-credit alias "
                    "from embedded series"
                )
                series = filename_series
            else:
                series = series or filename_series
            sequence = sequence or SequenceNumber.parse(groups[1])
            if len(groups) > 2:
                title = title or _clean_title(groups[2])
        elif re.search(r"\b(?:Annual|Special)\b", stem, re.IGNORECASE):
            subtype = "annual" if re.search(r"\bAnnual\b", stem, re.IGNORECASE) else "special"
            series = series or _clean_title(stem)
        else:
            if sequence is not None and series:
                subtype = "issue"
                identity_reasons.append(
                    "embedded comic issue number keeps item in issue classification"
                )
            elif edition_qualifiers:
                subtype = "collected-edition"
                base, qualifier = _split_edition_qualified_title(stem, edition_qualifiers)
                series = base
                title = qualifier or base
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
        reasons=("comic archive or strong issue/folder evidence", *identity_reasons),
        edition_qualifiers=edition_qualifiers,
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
    text = _TRAILING_SOURCE_SITE.sub("", text)

    def replace(match: re.Match[str]) -> str:
        return "" if _NOISE_TERMS.fullmatch(match.group(1).strip()) else match.group(0)

    return re.sub(r"\(([^()]*)\)", replace, text).strip(" -_")


def _comic_filename_semantics(text: str) -> tuple[str, int | None, tuple[str, ...]]:
    """Return identity-bearing comic filename text plus structured edition evidence."""
    text = _without_release_noise(text)
    year: int | None = None
    qualifiers: list[str] = []

    def replace_parenthetical(match: re.Match[str]) -> str:
        nonlocal year
        raw = match.group(1).strip()
        if pure_year := re.fullmatch(r"(?:19|20)\d{2}", raw):
            year = year or int(pure_year.group(0))
            return ""
        if compound := _compound_year_qualifier(raw):
            compound_year, qualifier = compound
            year = year or compound_year
            qualifiers.append(qualifier)
            return ""
        if _looks_like_edition_qualifier(raw):
            qualifiers.append(_clean_title(raw))
            return ""
        return match.group(0)

    cleaned = re.sub(r"\(([^()]*)\)", replace_parenthetical, text)
    cleaned = _clean_title(cleaned)
    return cleaned, year, tuple(dict.fromkeys(qualifiers))


def _compound_year_qualifier(value: str) -> tuple[int, str] | None:
    match = _COMPOUND_YEAR.fullmatch(value.strip())
    if not match or not _looks_like_edition_qualifier(match.group(2)):
        return None
    return int(match.group(1)), _clean_title(match.group(2))


def _looks_like_edition_qualifier(value: str) -> bool:
    return bool(_EDITION_QUALIFIER.search(value.strip()))


def _looks_like_source_site(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*"
            r"\.(?:com|net|org|info|io|co|me|cc)",
            value,
            re.IGNORECASE,
        )
    )


def _first_edition_qualifier(value: str) -> str | None:
    patterns = (
        r"Ultimate Collection",
        r"Collected Edition",
        r"Omnibus",
        r"TPB",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            return _clean_title(match.group(0))
    return None


def _merge_qualifiers(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    cleaned = _clean_title(value)
    if not cleaned or any(_normalize(item) == _normalize(cleaned) for item in values):
        return values
    return (*values, cleaned)


def _split_edition_qualified_title(
    stem: str,
    qualifiers: tuple[str, ...],
) -> tuple[str, str | None]:
    """Separate a base title from a removed parenthetical edition label."""
    base = _clean_title(stem)
    qualifier = qualifiers[0] if qualifiers else None
    return base, qualifier


def _series_creator_alias_base(value: str) -> str | None:
    """Recognize conservative trailing creator-credit variants used as series aliases."""
    match = re.match(r"^(.*?)\s+by\s+(.+)$", value, re.IGNORECASE)
    if not match:
        return None
    base, raw_credit = match.groups()
    parts = [part.strip() for part in re.split(r"\s+(?:and|&)\s+", raw_credit, flags=re.I)]
    if not parts or any(not part for part in parts):
        return None
    word_counts = [len(part.split()) for part in parts]
    if len(parts) == 1 and word_counts[0] < 2:
        return None
    if len(parts) > 1 and not any(count >= 2 for count in word_counts):
        return None
    if any(count > 4 for count in word_counts):
        return None
    return _normalize(_clean_title(base))


def _clean_title(value: str) -> str:
    value = value.replace("_", " ")
    value = re.sub(r"\s+-\s+", " - ", value)
    return re.sub(r"\s+", " ", value).strip(" -_")


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _trailing_year(value: str) -> int | None:
    match = re.search(r"\b((?:19|20)\d{2})$", value)
    return int(match.group(1)) if match else None


def _split_creator_credit(value: str) -> tuple[str | None, tuple[str, ...]]:
    """Split safe `Title by First Last [and First Last]` collection evidence.

    Requiring every credited name to contain at least two words avoids treating
    genuine titles such as `Batman by Gaslight` as creator credits.
    """
    match = re.match(r"^(.*?)\s+by\s+(.+)$", value, re.IGNORECASE)
    if not match:
        return None, ()
    title, raw_creators = match.groups()
    creators = tuple(
        _clean_title(part)
        for part in re.split(r"\s+(?:and|&)\s+", raw_creators, flags=re.IGNORECASE)
        if part.strip()
    )
    if not creators or any(len(name.split()) < 2 for name in creators):
        return None, ()
    return _clean_title(title), creators
