from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String


@dataclass(frozen=True)
class PdfFingerprint:
    page_count: int
    media_boxes: tuple[tuple[float, ...], ...]
    content_hashes: tuple[str, ...]
    resource_hashes: tuple[tuple[str, str], ...]


def create_pdf(path: Path, *, encrypted: bool = False, signed_marker: bool = False) -> Path:
    pdf = pikepdf.Pdf.new()
    for index in range(2):
        page = pdf.add_blank_page(page_size=(300, 400))
        image = pdf.make_stream(bytes([255, index * 80, 0] * 4))
        image.Type = Name.XObject
        image.Subtype = Name.Image
        image.Width = 2
        image.Height = 2
        image.ColorSpace = Name.DeviceRGB
        image.BitsPerComponent = 8
        page.Resources = Dictionary(XObject=Dictionary(Im0=image))
        page.Contents = pdf.make_stream(b"q 100 0 0 100 50 50 cm /Im0 Do Q\n")

    pdf.docinfo[Name.Title] = String("Fixture PDF")
    pdf.docinfo[Name.Author] = String("Alex Author")
    if signed_marker:
        signature = pdf.make_indirect(
            Dictionary(FT=Name.Sig, T=String("Synthetic signature marker"))
        )
        pdf.Root.AcroForm = Dictionary(Fields=Array([signature]))

    encryption = pikepdf.Encryption(owner="owner", user="reader", R=6) if encrypted else False
    pdf.save(path, encryption=encryption)
    return path


def has_signature_fields(pdf: pikepdf.Pdf) -> bool:
    acroform = pdf.Root.get("/AcroForm")
    if not acroform:
        return False
    return any(field.get("/FT") == Name.Sig for field in acroform.get("/Fields", []))


def fingerprint_pdf(pdf: pikepdf.Pdf) -> PdfFingerprint:
    content_hashes: list[str] = []
    resource_hashes: list[tuple[str, str]] = []
    boxes: list[tuple[float, ...]] = []
    for page_index, page in enumerate(pdf.pages):
        boxes.append(tuple(float(value) for value in page.MediaBox))
        contents = page.get("/Contents")
        streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
        content = b"".join(stream.read_bytes() for stream in streams if stream is not None)
        content_hashes.append(hashlib.sha256(content).hexdigest())
        xobjects = page.Resources.get("/XObject", {})
        for name, stream in xobjects.items():
            key = f"{page_index}:{name}"
            resource_hashes.append((key, hashlib.sha256(stream.read_bytes()).hexdigest()))
    return PdfFingerprint(
        page_count=len(pdf.pages),
        media_boxes=tuple(boxes),
        content_hashes=tuple(content_hashes),
        resource_hashes=tuple(resource_hashes),
    )


def update_metadata(source: Path, destination: Path, fields: dict[str, str]) -> None:
    with pikepdf.open(source) as pdf:
        if pdf.is_encrypted:
            raise ValueError("encrypted PDFs are not eligible for automatic metadata writes")
        if has_signature_fields(pdf):
            raise ValueError("signed PDFs are not eligible for automatic metadata writes")
        mapping = {
            "title": Name.Title,
            "author": Name.Author,
            "subject": Name.Subject,
            "keywords": Name.Keywords,
        }
        for field, value in fields.items():
            if field in mapping:
                pdf.docinfo[mapping[field]] = String(value)
        with pdf.open_metadata(set_pikepdf_as_editor=False) as metadata:
            if "title" in fields:
                metadata["dc:title"] = fields["title"]
            if "author" in fields:
                metadata["dc:creator"] = [fields["author"]]
            if "subject" in fields:
                metadata["dc:description"] = fields["subject"]
            if "keywords" in fields:
                metadata["pdf:Keywords"] = fields["keywords"]
            if "language" in fields:
                metadata["dc:language"] = fields["language"]
            if "identifier" in fields:
                metadata["dc:identifier"] = fields["identifier"]
            if "date" in fields:
                metadata["dc:date"] = fields["date"]
        pdf.save(destination)
