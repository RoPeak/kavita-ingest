from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType


class LockUnavailable(RuntimeError):
    pass


class ProcessLock:
    """Crash-safe advisory lock shared by apply, recovery, and rollback inspection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: int | None = None

    def __enter__(self) -> ProcessLock:
        if not sys.platform.startswith("linux"):
            raise LockUnavailable("apply locking is currently supported only on Linux")
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(handle)
            raise LockUnavailable(
                "another apply, recovery, or rollback process holds the state lock"
            ) from exc
        os.ftruncate(handle, 0)
        os.write(handle, f"pid={os.getpid()}\n".encode())
        os.fsync(handle)
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        if self._handle is None:
            return
        import fcntl

        fcntl.flock(self._handle, fcntl.LOCK_UN)
        os.close(self._handle)
        self._handle = None


def lock_path(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.name}.apply.lock")
