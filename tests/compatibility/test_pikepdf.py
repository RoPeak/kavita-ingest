from __future__ import annotations

import hashlib
from pathlib import Path

import pikepdf
import pytest

from compatibility.helpers.pdf_factory import (
    create_pdf,
    fingerprint_pdf,
    has_signature_fields,
    update_metadata,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_metadata_update_preserves_page_content_and_resource_payloads(tmp_path: Path) -> None:
    source = create_pdf(tmp_path / "source.pdf")
    output = tmp_path / "output.pdf"
    with pikepdf.open(source) as pdf:
        before = fingerprint_pdf(pdf)

    update_metadata(
        source,
        output,
        {
            "title": "Changed PDF",
            "author": "Casey Author",
            "subject": "Compatibility fixture",
            "keywords": "fixture, metadata",
            "language": "en-GB",
            "identifier": "urn:isbn:9781111111113",
            "date": "2025-06-07",
        },
    )

    with pikepdf.open(output) as pdf:
        after = fingerprint_pdf(pdf)
        assert str(pdf.docinfo["/Title"]) == "Changed PDF"
        assert str(pdf.docinfo["/Author"]) == "Casey Author"
        with pdf.open_metadata() as metadata:
            assert metadata["dc:language"] == "en-GB"
            assert metadata["dc:identifier"] == "urn:isbn:9781111111113"
            assert metadata["dc:date"] == "2025-06-07"
    assert after == before
    assert sha256(output) != sha256(source)


def test_encrypted_pdf_is_readable_with_password_but_blocked_for_writing(tmp_path: Path) -> None:
    source = create_pdf(tmp_path / "encrypted.pdf", encrypted=True)
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(source)
    with pikepdf.open(source, password="reader") as pdf:
        assert pdf.is_encrypted
        assert len(pdf.pages) == 2
    with pytest.raises((pikepdf.PasswordError, ValueError)):
        update_metadata(source, tmp_path / "output.pdf", {"title": "Blocked"})


def test_signature_field_is_detected_and_blocks_rewrite(tmp_path: Path) -> None:
    source = create_pdf(tmp_path / "signed-marker.pdf", signed_marker=True)
    with pikepdf.open(source) as pdf:
        assert has_signature_fields(pdf)
    with pytest.raises(ValueError, match="signed PDFs"):
        update_metadata(source, tmp_path / "output.pdf", {"title": "Blocked"})
