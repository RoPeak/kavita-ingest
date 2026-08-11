from __future__ import annotations

import errno
import hashlib
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class DestinationExists(FileExistsError):
    pass


class CommitUnsupported(OSError):
    pass


@dataclass(frozen=True, slots=True)
class PublicationProbe:
    supported: bool
    detail: str


class NoClobberFilesystem(Protocol):
    def ensure_directory(self, root: Path, directory: Path, mode: int = 0o755) -> None: ...

    def set_file_mode(self, path: Path, mode: int) -> None: ...

    def make_file_durable(self, path: Path) -> None: ...

    def commit(self, staged: Path, destination: Path) -> None: ...

    def durable_unlink(self, path: Path) -> None: ...

    def copy_for_archive(
        self, source: Path, archive: Path, expected_hash: str, directory_mode: int = 0o755
    ) -> None: ...

    def probe_no_clobber(self, root: Path) -> PublicationProbe: ...


@dataclass(frozen=True, slots=True)
class LinuxFilesystem:
    """Publish files with atomic hard-link creation and explicit durability barriers."""

    def ensure_directory(self, root: Path, directory: Path, mode: int = 0o755) -> None:
        resolved_root = root.resolve(strict=True)
        try:
            relative = directory.resolve(strict=False).relative_to(resolved_root)
        except ValueError as exc:
            raise OSError(f"planned directory escapes filesystem root: {directory}") from exc
        current = resolved_root
        for part in relative.parts:
            child = current / part
            try:
                child.mkdir()
            except FileExistsError as error:
                if not child.is_dir():
                    raise NotADirectoryError(
                        f"path component is not a directory: {child}"
                    ) from error
            else:
                try:
                    os.chmod(child, mode, follow_symlinks=False)
                except OSError:
                    child.rmdir()
                    _fsync_directory(current)
                    raise
                _fsync_directory(child)
                _fsync_directory(current)
            current = child

    def set_file_mode(self, path: Path, mode: int) -> None:
        if os.name == "posix":
            os.chmod(path, mode, follow_symlinks=False)

    def make_file_durable(self, path: Path) -> None:
        _fsync_file(path)
        _fsync_directory(path.parent)

    def commit(self, staged: Path, destination: Path) -> None:
        """Publish a sealed staged inode; callers must never write it after linking."""
        if not sys.platform.startswith("linux"):
            raise CommitUnsupported("atomic no-clobber publication is supported only on Linux")
        if not destination.parent.is_dir():
            raise NotADirectoryError(f"destination parent is unavailable: {destination.parent}")
        _fsync_directory(destination.parent)
        try:
            os.link(staged, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise DestinationExists(f"destination already exists: {destination}") from exc
        except OSError as exc:
            if exc.errno in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP}:
                raise CommitUnsupported(
                    "destination filesystem does not support atomic no-clobber hard-link commit"
                ) from exc
            raise
        _fsync_file(destination)
        _fsync_directory(destination.parent)
        os.unlink(staged)
        _fsync_directory(staged.parent)

    def durable_unlink(self, path: Path) -> None:
        os.unlink(path)
        _fsync_directory(path.parent)

    def copy_for_archive(
        self, source: Path, archive: Path, expected_hash: str, directory_mode: int = 0o755
    ) -> None:
        _ensure_from_existing_ancestor(archive.parent, directory_mode)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{archive.name}.", suffix=".archive-stage", dir=archive.parent
        )
        os.close(descriptor)
        staged = Path(name)
        try:
            with source.open("rb") as source_handle, staged.open("wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            if sha256_file(staged) != expected_hash:
                raise OSError("archival staging hash does not match planned source")
            self.commit(staged, archive)
        finally:
            staged.unlink(missing_ok=True)

    def probe_no_clobber(self, root: Path) -> PublicationProbe:
        if not sys.platform.startswith("linux"):
            return PublicationProbe(False, "atomic no-clobber publication is Linux-only")
        if not root.is_dir():
            return PublicationProbe(False, f"configured root is unavailable: {root}")
        try:
            with tempfile.TemporaryDirectory(prefix=".kavita-ingest-doctor-", dir=root) as name:
                directory = Path(name)
                staged = directory / "staged"
                destination = directory / "destination"
                staged.write_bytes(b"kavita-ingest-publication-probe")
                self.make_file_durable(staged)
                os.link(staged, destination, follow_symlinks=False)
                try:
                    os.link(staged, destination, follow_symlinks=False)
                except FileExistsError:
                    pass
                else:
                    return PublicationProbe(False, "existing destination was not protected")
                if staged.stat().st_ino != destination.stat().st_ino:
                    return PublicationProbe(False, "publication did not preserve the staged inode")
                _fsync_file(destination)
                _fsync_directory(directory)
            _fsync_directory(root)
        except OSError as exc:
            return PublicationProbe(False, f"hard-link publication probe failed: {exc}")
        return PublicationProbe(True, "atomic hard-link no-clobber publication is supported")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_from_existing_ancestor(directory: Path, mode: int) -> None:
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    if not current.is_dir():
        raise NotADirectoryError(f"path component is not a directory: {current}")
    for child in reversed(missing):
        child.mkdir()
        try:
            os.chmod(child, mode, follow_symlinks=False)
        except OSError:
            child.rmdir()
            _fsync_directory(child.parent)
            raise
        _fsync_directory(child)
        _fsync_directory(child.parent)
