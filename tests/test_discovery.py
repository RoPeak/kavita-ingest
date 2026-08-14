from __future__ import annotations

from pathlib import Path

from kavita_ingest.discovery import detect_signature, discover, inspect_source, is_multivolume_name
from kavita_ingest.domain import SourceFormat


def test_discovery_is_sorted_and_excludes_destination_roots(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    ignored = incoming / "staging"
    ignored.mkdir(parents=True)
    (incoming / "z.pdf").write_bytes(b"%PDF-1.4\n")
    (incoming / "a.epub").write_bytes(b"PK\x03\x04broken")
    (incoming / "notes.txt").write_text("ignore", encoding="utf-8")
    (ignored / "hidden.pdf").write_bytes(b"%PDF-1.4\n")
    assert [path.name for path in discover(incoming, (ignored,))] == ["a.epub", "z.pdf"]



def test_discovery_naturally_sorts_numbered_media(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    for name in ("Watchmen #10.pdf", "Watchmen #2.pdf", "Watchmen #1.pdf"):
        (incoming / name).write_bytes(b"%PDF-1.4\n")

    assert [path.name for path in discover(incoming)] == [
        "Watchmen #1.pdf",
        "Watchmen #2.pdf",
        "Watchmen #10.pdf",
    ]

def test_signature_wins_over_extension_and_hash_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "misnamed.epub"
    path.write_bytes(b"%PDF-1.7\nfixture")
    signature, format_ = detect_signature(path)
    source = inspect_source(path)
    assert (signature, format_) == ("pdf", SourceFormat.PDF)
    assert source.sha256 == "f581fc87f30296eff11777c3ce1b9a8b7077071ad8abedfcba317fef0c807224"


def test_multivolume_patterns_are_detected() -> None:
    assert is_multivolume_name("comic.part01.rar")
    assert is_multivolume_name("comic.r00")
    assert not is_multivolume_name("comic.cbr")
