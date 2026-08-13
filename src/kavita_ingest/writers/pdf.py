from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

import pikepdf
from lxml import etree
from pikepdf import Name

from ..calibre import (
    require_safe_calibre_executable,
    safe_calibre_environment,
)
from .common import VerificationResult

PDF_CALIBRE_FIELDS = frozenset(
    {
        "title",
        "authors",
        "publisher",
        "series",
        "series_index",
        "description",
        "subjects",
        "language",
        "date",
        "identifiers",
    }
)

PDF_XMP_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "xmpidq": "http://ns.adobe.com/xmp/Identifier/qual/1.0/",
    "calibre": "http://calibre-ebook.com/xmp-namespace",
    "calibreSI": "http://calibre-ebook.com/xmp-namespace-series-index",
    "pdfx": "http://ns.adobe.com/pdfx/1.3/",
    "prism": "http://prismstandard.org/namespaces/basic/2.0/",
}


@dataclass(frozen=True, slots=True)
class PdfSemanticFingerprint:
    page_count: int
    media_boxes: tuple[tuple[float, ...], ...]
    content_hashes: tuple[str, ...]
    resource_hashes: tuple[tuple[str, str], ...]


def write_pdf_metadata(
    source: Path,
    destination: Path,
    *,
    fields: Mapping[str, object],
    ebook_meta: str = "ebook-meta",
) -> VerificationResult:
    unsupported = set(fields) - PDF_CALIBRE_FIELDS

    if unsupported:
        raise ValueError(f"unsupported PDF metadata fields: {sorted(unsupported)}")

    require_safe_calibre_executable(ebook_meta)

    with pikepdf.open(source) as pdf:
        _require_writable(pdf)

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".pdf",
        dir=destination.parent,
    )

    os.close(fd)
    staged = Path(name)

    try:
        shutil.copy2(
            source,
            staged,
        )

        if fields:
            subprocess.run(
                _ebook_meta_command(
                    ebook_meta,
                    staged,
                    fields,
                ),
                check=True,
                capture_output=True,
                text=True,
                env=safe_calibre_environment(),
            )

        result = verify_pdf(
            source,
            staged,
            fields,
        )

        result.require_valid()

        os.replace(
            staged,
            destination,
        )

        return result

    finally:
        staged.unlink(missing_ok=True)


def verify_pdf(
    source: Path,
    candidate: Path,
    fields: Mapping[str, object],
) -> VerificationResult:
    errors: list[str] = []

    unsupported = set(fields) - PDF_CALIBRE_FIELDS

    if unsupported:
        errors.append(f"unsupported PDF metadata fields: {sorted(unsupported)}")

    try:
        with (
            pikepdf.open(source) as before,
            pikepdf.open(candidate) as after,
        ):
            _require_writable(before)

            if _fingerprint(before) != _fingerprint(after):
                errors.append("PDF page/content/resource semantic fingerprint changed")

            if fields:
                root = _read_xmp(after)

                _verify_xmp_fields(
                    root,
                    fields,
                    errors,
                )

    except (
        OSError,
        pikepdf.PdfError,
        etree.XMLSyntaxError,
        ValueError,
    ) as exc:
        errors.append(str(exc))

    return VerificationResult(
        not errors,
        (
            "page_tree",
            "decoded_content_hashes",
            "resource_hashes",
            "calibre_xmp_readback",
        ),
        tuple(errors),
    )


def _ebook_meta_command(
    executable: str,
    pdf: Path,
    fields: Mapping[str, object],
) -> list[str]:
    unsupported = set(fields) - PDF_CALIBRE_FIELDS

    if unsupported:
        raise ValueError(f"unsupported PDF metadata fields: {sorted(unsupported)}")

    command = [
        executable,
        str(pdf),
    ]

    simple = {
        "title": "--title",
        "publisher": "--publisher",
        "series": "--series",
        "series_index": "--index",
        "description": "--comments",
        "language": "--language",
        "date": "--date",
    }

    for field, option in simple.items():
        value = fields.get(field)

        if value is not None:
            command.extend(
                [
                    option,
                    str(value),
                ]
            )

    authors = fields.get("authors")

    if authors is not None:
        if not isinstance(authors, Sequence) or isinstance(authors, (str, bytes)):
            raise ValueError("PDF authors must be a sequence of names")

        command.extend(
            [
                "--authors",
                " & ".join(str(author) for author in authors),
            ]
        )

    subjects = fields.get("subjects")

    if subjects is not None:
        if not isinstance(subjects, Sequence) or isinstance(subjects, (str, bytes)):
            raise ValueError("PDF subjects must be a sequence")

        if subjects:
            command.extend(
                [
                    "--tags",
                    ", ".join(str(subject) for subject in subjects),
                ]
            )

    identifiers = fields.get("identifiers")

    if identifiers is not None:
        if not isinstance(
            identifiers,
            Mapping,
        ):
            raise ValueError("PDF identifiers must be a mapping")

        rendered = {str(key).casefold(): str(value) for key, value in identifiers.items()}

        for key, value in sorted(rendered.items()):
            command.extend(
                [
                    "--identifier",
                    f"{key}:{value}",
                ]
            )

        isbn = _preferred_isbn(rendered)

        if isbn is not None:
            command.extend(
                [
                    "--isbn",
                    isbn,
                ]
            )

    return command


def _preferred_isbn(
    identifiers: Mapping[str, str],
) -> str | None:
    for key in (
        "isbn_13",
        "isbn13",
        "isbn",
        "isbn_10",
        "isbn10",
    ):
        value = identifiers.get(key)

        if value:
            return str(value)

    return None


def _read_xmp(
    pdf: pikepdf.Pdf,
) -> etree._Element:
    stream = pdf.Root.get("/Metadata")

    if stream is None:
        raise ValueError("PDF metadata write produced no XMP metadata stream")

    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        remove_blank_text=False,
    )

    return etree.fromstring(
        stream.read_bytes(),
        parser,
    )


def _verify_xmp_fields(
    root: etree._Element,
    fields: Mapping[str, object],
    errors: list[str],
) -> None:
    if "title" in fields:
        title_actual = _xmp_alt(
            root,
            "dc:title",
        )

        if title_actual != str(fields["title"]):
            errors.append("PDF title XMP read-back mismatch")

    authors = fields.get("authors")

    if authors is not None:
        if not isinstance(authors, Sequence) or isinstance(authors, (str, bytes)):
            errors.append("PDF authors expectation is invalid")
        else:
            authors_expected = tuple(str(author) for author in authors)

            authors_actual = _xmp_sequence(
                root,
                "dc:creator",
            )

            if authors_actual != authors_expected:
                errors.append("PDF authors XMP read-back mismatch")

    if "publisher" in fields:
        publishers = _xmp_sequence(
            root,
            "dc:publisher",
        )

        publisher_actual = publishers[0] if publishers else ""

        if publisher_actual != str(fields["publisher"]):
            errors.append("PDF publisher XMP read-back mismatch")

    if "description" in fields:
        description_actual = _xmp_alt(
            root,
            "dc:description",
        )

        if description_actual != str(fields["description"]):
            errors.append("PDF description XMP read-back mismatch")

    subjects = fields.get("subjects")

    if subjects is not None:
        if not isinstance(subjects, Sequence) or isinstance(subjects, (str, bytes)):
            errors.append("PDF subjects expectation is invalid")
        else:
            subjects_expected = {str(subject) for subject in subjects}

            subjects_actual = set(
                _xmp_sequence(
                    root,
                    "dc:subject",
                )
            )

            if subjects_actual != subjects_expected:
                errors.append("PDF subjects XMP read-back mismatch")

    if "language" in fields:
        languages = _xmp_sequence(
            root,
            "dc:language",
        )

        language_actual = languages[0] if languages else ""

        if not _same_language(
            str(fields["language"]),
            language_actual,
        ):
            errors.append("PDF language XMP read-back mismatch")

    if "date" in fields:
        dates = _xmp_sequence(
            root,
            "dc:date",
        )

        date_actual = dates[0] if dates else ""

        if not _same_date(
            str(fields["date"]),
            date_actual,
        ):
            errors.append("PDF date XMP read-back mismatch")

    if "series" in fields:
        series_actual = str(
            root.xpath(
                "string(.//calibre:series/rdf:value[1])",
                namespaces=PDF_XMP_NS,
            )
        )

        if series_actual != str(fields["series"]):
            errors.append("PDF series XMP read-back mismatch")

    if "series_index" in fields:
        series_index_actual = str(
            root.xpath(
                "string(.//calibre:series/calibreSI:series_index[1])",
                namespaces=PDF_XMP_NS,
            )
        )

        if not _same_number(
            str(fields["series_index"]),
            series_index_actual,
        ):
            errors.append("PDF series index XMP read-back mismatch")

    identifiers = fields.get("identifiers")

    if identifiers is not None:
        if not isinstance(
            identifiers,
            Mapping,
        ):
            errors.append("PDF identifiers expectation is invalid")
        else:
            identifiers_expected = {
                str(key).casefold(): str(value) for key, value in identifiers.items()
            }

            identifiers_actual = _xmp_identifiers(root)

            if any(
                identifiers_actual.get(key) != value for key, value in identifiers_expected.items()
            ):
                errors.append("PDF identifiers XMP read-back mismatch")

            isbn = _preferred_isbn(identifiers_expected)

            if isbn is not None:
                visible = _kavita_isbn(root)

                if not _same_isbn(
                    isbn,
                    visible,
                ):
                    errors.append("PDF Kavita ISBN read-back mismatch")


def _xmp_alt(
    root: etree._Element,
    tag: str,
) -> str:
    value = root.xpath(
        f"string(.//{tag}/rdf:Alt/rdf:li[1])",
        namespaces=PDF_XMP_NS,
    )

    return str(value)


def _xmp_sequence(
    root: etree._Element,
    tag: str,
) -> tuple[str, ...]:
    values = root.xpath(
        f".//{tag}/rdf:Seq/rdf:li/text() | .//{tag}/rdf:Bag/rdf:li/text()",
        namespaces=PDF_XMP_NS,
    )

    return tuple(str(value) for value in values)


def _xmp_identifiers(
    root: etree._Element,
) -> dict[str, str]:
    output: dict[str, str] = {}

    values = root.xpath(
        ".//xmp:Identifier/rdf:Bag/rdf:li",
        namespaces=PDF_XMP_NS,
    )

    for item in values:
        scheme = str(
            item.xpath(
                "string(.//xmpidq:Scheme[1])",
                namespaces=PDF_XMP_NS,
            )
        ).strip()

        value = str(
            item.xpath(
                "string(.//rdf:value[1])",
                namespaces=PDF_XMP_NS,
            )
        ).strip()

        if scheme and value:
            output[scheme.casefold()] = value

    return output


def _kavita_isbn(
    root: etree._Element,
) -> str:
    pdfx = str(
        root.xpath(
            "string(.//pdfx:isbn[1])",
            namespaces=PDF_XMP_NS,
        )
    ).strip()

    if pdfx:
        return pdfx

    return str(
        root.xpath(
            "string(.//prism:isbn[1])",
            namespaces=PDF_XMP_NS,
        )
    ).strip()


def _same_language(
    expected: str,
    actual: str,
) -> bool:
    expected_value = expected.strip().replace("_", "-").casefold()

    actual_value = actual.strip().replace("_", "-").casefold()

    if not actual_value:
        return False

    if expected_value == actual_value:
        return True

    return expected_value.split("-", 1)[0] == actual_value.split("-", 1)[0]


def _same_date(
    expected: str,
    actual: str,
) -> bool:
    expected = expected.strip()
    actual = actual.strip()

    if re.fullmatch(
        r"\d{4}",
        expected,
    ):
        return actual[:4] == expected

    if re.fullmatch(
        r"\d{4}-\d{2}",
        expected,
    ):
        return actual[:7] == expected

    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}",
        expected,
    ):
        return actual[:10] == expected

    return actual == expected


def _same_number(
    expected: str,
    actual: str,
) -> bool:
    try:
        return Decimal(expected) == Decimal(actual)
    except InvalidOperation:
        return expected == actual


def _same_isbn(
    expected: str,
    actual: str,
) -> bool:
    def normalized(
        value: str,
    ) -> str:
        return re.sub(
            r"[^0-9Xx]",
            "",
            value,
        ).upper()

    return bool(actual.strip()) and normalized(expected) == normalized(actual)


def _require_writable(
    pdf: pikepdf.Pdf,
) -> None:
    if pdf.is_encrypted:
        raise ValueError("encrypted PDFs are ineligible for metadata writes")

    acroform = pdf.Root.get("/AcroForm")

    signature_fields: Any = (
        acroform.get(
            "/Fields",
            [],
        )
        if acroform
        else []
    )

    if any(field.get("/FT") == Name.Sig for field in signature_fields):
        raise ValueError("signature-bearing PDFs are ineligible for metadata writes")


def require_pdf_write_eligible(
    path: Path,
) -> None:
    with pikepdf.open(path) as pdf:
        _require_writable(pdf)


def _stream_payload(
    stream: Any,
) -> bytes:
    try:
        return b"decoded\0" + cast(
            bytes,
            stream.read_bytes(),
        )

    except (
        pikepdf.PdfError,
        ValueError,
    ):
        filters = str(
            stream.get(
                "/Filter",
                "",
            )
        )

        return (
            b"raw\0"
            + filters.encode(
                "utf-8",
                errors="replace",
            )
            + b"\0"
            + cast(
                bytes,
                stream.read_raw_bytes(),
            )
        )


def _fingerprint(
    pdf: pikepdf.Pdf,
) -> PdfSemanticFingerprint:
    boxes: list[tuple[float, ...]] = []

    content: list[str] = []

    resources: list[tuple[str, str]] = []

    for page_index, page in enumerate(pdf.pages):
        boxes.append(tuple(float(value) for value in page.MediaBox))

        streams = page.get("/Contents")

        values = (
            list(streams)
            if isinstance(
                streams,
                pikepdf.Array,
            )
            else [streams]
        )

        payload = b"".join(_stream_payload(stream) for stream in values if stream is not None)

        content.append(hashlib.sha256(payload).hexdigest())

        for key, stream in page.Resources.get(
            "/XObject",
            {},
        ).items():
            resources.append(
                (
                    f"{page_index}:{key}",
                    hashlib.sha256(_stream_payload(stream)).hexdigest(),
                )
            )

    return PdfSemanticFingerprint(
        len(pdf.pages),
        tuple(boxes),
        tuple(content),
        tuple(resources),
    )
