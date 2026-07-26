from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import TracebackType
from typing import Self

try:
    import fcntl
except ImportError as exc:  # pragma: no cover - the tomorrow beta targets macOS/Linux
    raise RuntimeError("CouncilLogic run locking requires a POSIX host") from exc


class RunLock:
    """Crash-safe, process-wide exclusive lock for one council run."""

    def __init__(self, data_dir: str | Path, run_id: str) -> None:
        safe_id = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        lock_dir = Path(data_dir) / "locks"
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(lock_dir, 0o700)
        self.path = lock_dir / f"{safe_id}.lock"
        self._descriptor: int | None = None

    def __enter__(self) -> Self:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise RuntimeError(
                "This council run is already active in another process"
            ) from None
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._descriptor is not None:
            try:
                fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self._descriptor)
                self._descriptor = None


class ServiceLock:
    """Common exclusive lock for CLI/service ownership of one data directory."""

    def __init__(self, data_dir: str | Path) -> None:
        directory = Path(data_dir).expanduser()
        if directory.is_symlink():
            raise ValueError("service data directory must not be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        lock_dir = directory / "locks"
        if lock_dir.is_symlink():
            raise ValueError("service lock directory must not be a symlink")
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(lock_dir, 0o700)
        self.path = lock_dir / "council-service.lock"
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise RuntimeError(
                "Another Council process already owns this data directory"
            ) from None
        self._descriptor = descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
