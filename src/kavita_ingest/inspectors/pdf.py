from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pikepdf

from ..domain import Evidence, InspectionResult, InspectionStatus, SourceFormat


def inspect_pdf(path: Path) -> InspectionResult:
    try:
        with pikepdf.open(path) as pdf:
            signed = _contains_signature_field(pdf)
            document_info = _document_info(pdf)
            metadata: dict[str, Any] = {
                "page_count": len(pdf.pages),
                "encrypted": pdf.is_encrypted,
                "signature_fields": signed,
                "document_info": document_info,
                "content_stream_sha256": [_page_content_hash(page) for page in pdf.pages],
            }
            warnings = (
                ("PDF contains signature fields and must not be rewritten.",) if signed else ()
            )
            evidence = tuple(
                Evidence(key, value, value, "pdf-document-info", 0.9)
                for key, value in document_info.items()
            )
            return InspectionResult(
                InspectionStatus.OK, SourceFormat.PDF, metadata, evidence, warnings
            )
    except pikepdf.PasswordError as exc:
        return InspectionResult(
            InspectionStatus.BLOCKED,
            SourceFormat.PDF,
            error_code="encrypted_pdf",
            error_message=f"Encrypted PDF is read-only/unsupported: {exc}",
        )
    except (OSError, pikepdf.PdfError, ValueError) as exc:
        return InspectionResult(
            InspectionStatus.FAILED,
            SourceFormat.PDF,
            error_code="invalid_pdf",
            error_message=f"{type(exc).__name__}: {exc}",
        )


def _document_info(pdf: pikepdf.Pdf) -> dict[str, str]:
    keys = {"/Title": "title", "/Author": "author", "/Subject": "subject", "/Keywords": "keywords"}
    return {
        output: str(pdf.docinfo[key])
        for key, output in keys.items()
        if key in pdf.docinfo and pdf.docinfo[key] is not None
    }


def _page_content_hash(page: Any) -> str:
    contents = page.obj.get("/Contents")
    digest = hashlib.sha256()
    if contents is None:
        return digest.hexdigest()
    streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
    for stream in streams:
        digest.update(stream.read_bytes())
    return digest.hexdigest()


def _contains_signature_field(pdf: pikepdf.Pdf) -> bool:
    acroform = pdf.Root.get("/AcroForm")
    if acroform is None:
        return False
    fields = list(acroform.get("/Fields", []))
    while fields:
        field = fields.pop()
        if str(field.get("/FT", "")) == "/Sig":
            return True
        fields.extend(field.get("/Kids", []))
    return False
