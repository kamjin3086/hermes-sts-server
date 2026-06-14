from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger(__name__)

_lock_handle: BinaryIO | None = None


def acquire_singleton_lock(path: Path) -> None:
    """Keep only one STS server process alive for this workspace."""
    global _lock_handle
    if _lock_handle is not None:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        _try_lock(handle)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            f"Another hermes-sts-server instance is already running or holding {path}"
        ) from exc

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()).encode("ascii"))
    handle.flush()
    _lock_handle = handle
    atexit.register(_release_singleton_lock)
    logger.info("Acquired STS singleton lock path=%s pid=%s", path, os.getpid())


def _try_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_singleton_lock() -> None:
    global _lock_handle
    if _lock_handle is None:
        return
    handle = _lock_handle
    _lock_handle = None
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
