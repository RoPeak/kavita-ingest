from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pikepdf
from pikepdf import Name, String

from .common import VerificationResult


@dataclass(frozen=True, slots=True)
class PdfSemanticFingerprint:
    page_count: int
    media_boxes: tuple[tuple[float, ...], ...]
    content_hashes: tuple[str, ...]
    resource_hashes: tuple[tuple[str, str], ...]


def write_pdf_metadata(
    source: Path, destination: Path, *, fields: dict[str, str]
) -> VerificationResult:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".pdf", dir=destination.parent
    )
    os.close(fd)
    staged = Path(name)
    try:
        with pikepdf.open(source) as pdf:
            _require_writable(pdf)
            mapping = {
                "title": Name.Title,
                "author": Name.Author,
                "subject": Name.Subject,
                "keywords": Name.Keywords,
            }
            for field, docinfo_key in mapping.items():
                if field in fields:
                    pdf.docinfo[docinfo_key] = String(fields[field])
            with pdf.open_metadata(set_pikepdf_as_editor=False) as metadata:
                xmp = {
                    "title": "dc:title",
                    "subject": "dc:description",
                    "keywords": "pdf:Keywords",
                    "language": "dc:language",
                    "identifier": "dc:identifier",
                    "date": "dc:date",
                }
                for field, xmp_key in xmp.items():
                    if field in fields:
                        metadata[xmp_key] = fields[field]
                if "author" in fields:
                    metadata["dc:creator"] = [fields["author"]]
            pdf.save(staged)
        result = verify_pdf(source, staged, fields)
        result.require_valid()
        os.replace(staged, destination)
        return result
    finally:
        staged.unlink(missing_ok=True)


def verify_pdf(source: Path, candidate: Path, fields: dict[str, str]) -> VerificationResult:
    errors: list[str] = []
    try:
        with pikepdf.open(source) as before, pikepdf.open(candidate) as after:
            _require_writable(before)
            if _fingerprint(before) != _fingerprint(after):
                errors.append("PDF page/content/resource semantic fingerprint changed")
            mapping = {
                "title": Name.Title,
                "author": Name.Author,
                "subject": Name.Subject,
                "keywords": Name.Keywords,
            }
            for field, docinfo_key in mapping.items():
                if field in fields and str(after.docinfo.get(docinfo_key, "")) != fields[field]:
                    errors.append(f"PDF {field} read-back mismatch")
            with after.open_metadata() as metadata:
                xmp = {"language": "dc:language", "identifier": "dc:identifier", "date": "dc:date"}
                for field, xmp_key in xmp.items():
                    if field in fields and str(metadata.get(xmp_key, "")) != fields[field]:
                        errors.append(f"PDF {field} XMP read-back mismatch")
    except (OSError, pikepdf.PdfError, ValueError) as exc:
        errors.append(str(exc))
    return VerificationResult(
        not errors,
        ("page_tree", "decoded_content_hashes", "resource_hashes", "metadata_readback"),
        tuple(errors),
    )


def _require_writable(pdf: pikepdf.Pdf) -> None:
    if pdf.is_encrypted:
        raise ValueError("encrypted PDFs are ineligible for metadata writes")
    acroform = pdf.Root.get("/AcroForm")
    signature_fields: Any = acroform.get("/Fields", []) if acroform else []
    if any(field.get("/FT") == Name.Sig for field in signature_fields):
        raise ValueError("signature-bearing PDFs are ineligible for metadata writes")


def require_pdf_write_eligible(path: Path) -> None:
    with pikepdf.open(path) as pdf:
        _require_writable(pdf)


def _fingerprint(pdf: pikepdf.Pdf) -> PdfSemanticFingerprint:
    boxes: list[tuple[float, ...]] = []
    content: list[str] = []
    resources: list[tuple[str, str]] = []
    for page_index, page in enumerate(pdf.pages):
        boxes.append(tuple(float(value) for value in page.MediaBox))
        streams = page.get("/Contents")
        values = list(streams) if isinstance(streams, pikepdf.Array) else [streams]
        payload = b"".join(stream.read_bytes() for stream in values if stream is not None)
        content.append(hashlib.sha256(payload).hexdigest())
        for key, stream in page.Resources.get("/XObject", {}).items():
            resources.append(
                (f"{page_index}:{key}", hashlib.sha256(stream.read_bytes()).hexdigest())
            )
    return PdfSemanticFingerprint(len(pdf.pages), tuple(boxes), tuple(content), tuple(resources))
