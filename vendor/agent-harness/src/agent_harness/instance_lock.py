from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import IO


class InstanceLockedError(RuntimeError):
    pass


class InstanceLock:
    """OS-backed, process-lifetime exclusive lock for one harness home."""

    def __init__(self, path: Path, address: str = "") -> None:
        self.path = path
        self.address = address
        self._file: IO[str] | None = None
        self.started_at = time.time()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt
                file.seek(0)
                if not file.read(1):
                    file.write("\0"); file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            file.seek(0)
            owner = file.read().strip("\0\n ") or "unknown owner"
            file.close()
            raise InstanceLockedError(f"harness home is already locked: {owner}") from exc
        self._file = file
        self.heartbeat()

    def heartbeat(self) -> None:
        if self._file is None:
            return
        payload = {"pid": os.getpid(), "started_at": self.started_at, "heartbeat_at": time.time(), "address": self.address}
        self._file.seek(0); self._file.truncate(); self._file.write(json.dumps(payload)); self._file.flush()
        os.fsync(self._file.fileno())

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0); msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close(); self._file = None

    @property
    def held(self) -> bool:
        return self._file is not None
