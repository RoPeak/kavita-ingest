from __future__ import annotations

import hashlib
import stat
import struct
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from lxml import etree

from .archive_safety import ArchiveLimits, UnsafeArchive, validate_inventory

MIMETYPE = b"application/epub+zip"
CONTAINER = "META-INF/container.xml"
XML_METADATA_MAX_BYTES = 8 * 1024 * 1024
CONTAINER_NS = {"container": "urn:oasis:names:tc:opendocument:xmlns:container"}
OPF_NS = {"opf": "http://www.idpf.org/2007/opf"}
_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")


class EpubDisposition(StrEnum):
    VALID = "valid"
    REPAIRABLE = "repairable"
    BROKEN = "broken"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class EpubFinding:
    code: str
    message: str
    repairable: bool = False


@dataclass(frozen=True, slots=True)
class EpubValidation:
    disposition: EpubDisposition
    package_path: str | None
    findings: tuple[EpubFinding, ...]
    member_hashes: dict[str, str]

    @property
    def valid(self) -> bool:
        return self.disposition is EpubDisposition.VALID

    @property
    def may_repair(self) -> bool:
        return self.disposition is EpubDisposition.REPAIRABLE

    def require_valid(self) -> None:
        if not self.valid:
            raise ValueError("; ".join(item.message for item in self.findings))


def validate_epub_container(path: Path, limits: ArchiveLimits) -> EpubValidation:
    """Perform bounded OCF/ZIP validation without changing the EPUB."""
    findings: list[EpubFinding] = []
    package_path: str | None = None
    member_hashes: dict[str, str] = {}
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("EPUB source must be a regular non-symlink file")
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            encrypted_names = {item.filename for item in members if item.flag_bits & 1}
            validate_inventory(
                members,
                limits,
                link_names={
                    item.filename
                    for item in members
                    if stat.S_ISLNK((item.external_attr >> 16) & 0xFFFF)
                },
            )
            if encrypted_names:
                findings.append(
                    EpubFinding(
                        "encrypted_archive_member",
                        "EPUB contains ZIP-encrypted members; encrypted content is unsupported",
                    )
                )
                return EpubValidation(
                    EpubDisposition.UNSUPPORTED, None, tuple(findings), {}
                )
            names = archive.namelist()
            if "META-INF/encryption.xml" in names:
                findings.append(
                    EpubFinding(
                        "encrypted_publication",
                        "EPUB declares encrypted content; DRM/encryption is unsupported",
                    )
                )
            if "META-INF/signatures.xml" in names:
                findings.append(
                    EpubFinding(
                        "signed_publication",
                        "EPUB contains signatures.xml; signature-sensitive repair is refused",
                    )
                )
            _check_mimetype(path, archive, findings)
            if CONTAINER not in names:
                raise ValueError("EPUB META-INF/container.xml is missing")
            container_payload = _bounded_read(archive, CONTAINER)
            container = _xml(container_payload)
            roots = container.xpath("//container:rootfile/@full-path", namespaces=CONTAINER_NS)
            if not roots:
                raise ValueError("EPUB does not identify a package document")
            if len(roots) > 1:
                findings.append(
                    EpubFinding(
                        "multiple_package_documents",
                        "EPUB identifies multiple package documents; "
                        "select/review the rendition manually",
                    )
                )
            package_path = _safe_member_name(str(roots[0]), "package document")
            if package_path not in names:
                raise ValueError(f"EPUB package document is missing: {package_path}")
            package = _xml(_bounded_read(archive, package_path))
            if etree.QName(package).localname != "package":
                raise ValueError("EPUB package document has no package root")
            _validate_manifest(package, package_path, set(names))
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ValueError(f"EPUB ZIP CRC verification failed: {bad_member}")
            for member in members:
                if not member.is_dir():
                    member_hashes[member.filename] = hashlib.sha256(
                        archive.read(member)
                    ).hexdigest()
    except (
        OSError,
        KeyError,
        ValueError,
        NotImplementedError,
        zipfile.BadZipFile,
        etree.XMLSyntaxError,
        UnsafeArchive,
    ) as exc:
        findings.append(EpubFinding("invalid_epub", str(exc)))

    unsupported = any(
        finding.code in {"encrypted_publication", "signed_publication"}
        for finding in findings
    )
    if unsupported:
        disposition = EpubDisposition.UNSUPPORTED
    elif any(finding.code == "multiple_package_documents" for finding in findings):
        disposition = EpubDisposition.UNCERTAIN
    elif findings and all(finding.repairable for finding in findings):
        disposition = EpubDisposition.REPAIRABLE
    elif findings:
        disposition = EpubDisposition.BROKEN
    else:
        disposition = EpubDisposition.VALID
    return EpubValidation(disposition, package_path, tuple(findings), member_hashes)


def _check_mimetype(
    path: Path, archive: zipfile.ZipFile, findings: list[EpubFinding]
) -> None:
    members = archive.infolist()
    matching = [item for item in members if item.filename == "mimetype"]
    if len(matching) != 1:
        findings.append(
            EpubFinding(
                "mimetype_count",
                "EPUB must contain exactly one mimetype entry",
                repairable=len(matching) == 1,
            )
        )
        return
    info = matching[0]
    if archive.read(info) != MIMETYPE:
        findings.append(EpubFinding("mimetype_value", "EPUB mimetype value is not exact"))
    if not members or members[0].filename != "mimetype":
        findings.append(
            EpubFinding(
                "mimetype_not_first",
                "EPUB mimetype is not the first ZIP entry",
                repairable=True,
            )
        )
    if info.header_offset != 0:
        findings.append(
            EpubFinding(
                "mimetype_not_at_start",
                "EPUB contains data before the mimetype local header",
                repairable=True,
            )
        )
    if info.compress_type != zipfile.ZIP_STORED:
        findings.append(
            EpubFinding(
                "mimetype_compressed",
                "EPUB mimetype entry is compressed",
                repairable=True,
            )
        )
    with path.open("rb") as stream:
        stream.seek(info.header_offset)
        raw = stream.read(_LOCAL_HEADER.size)
        if len(raw) != _LOCAL_HEADER.size:
            raise ValueError("EPUB mimetype local header is truncated")
        fields = _LOCAL_HEADER.unpack(raw)
        signature, flags, compression = fields[0], fields[2], fields[3]
        name_length, extra_length = fields[-2], fields[-1]
        name = stream.read(name_length)
        if signature != 0x04034B50 or name != b"mimetype":
            raise ValueError("EPUB mimetype local header is invalid")
        if flags & 1:
            raise ValueError("EPUB mimetype entry is encrypted")
        if flags & 0x08:
            findings.append(
                EpubFinding(
                    "mimetype_data_descriptor",
                    "EPUB mimetype local header uses a data descriptor",
                    repairable=True,
                )
            )
        if compression != zipfile.ZIP_STORED:
            # Already represented by the central-directory finding.
            pass
        if extra_length:
            findings.append(
                EpubFinding(
                    "mimetype_extra_field",
                    "EPUB mimetype local header contains an extra field",
                    repairable=True,
                )
            )


def _bounded_read(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > XML_METADATA_MAX_BYTES:
        raise ValueError(f"EPUB metadata entry exceeds size limit: {name}")
    return archive.read(info)


def _xml(data: bytes) -> etree._Element:
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))


def _safe_member_name(value: str, label: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise ValueError(f"EPUB {label} has an unsafe path: {value!r}")
    return normalized


def _validate_manifest(package: etree._Element, package_path: str, names: set[str]) -> None:
    base = PurePosixPath(package_path).parent
    hrefs = package.xpath("//opf:manifest/opf:item/@href", namespaces=OPF_NS)
    for href in hrefs:
        parsed = urlsplit(str(href))
        if parsed.scheme or parsed.netloc or parsed.query:
            raise ValueError(f"EPUB manifest item has an unsupported URI: {href!r}")
        clean = _safe_member_name(unquote(parsed.path), "manifest item")
        resolved = str(base / PurePosixPath(clean)) if str(base) != "." else clean
        if resolved not in names:
            raise ValueError(f"EPUB manifest resource is missing: {resolved}")
