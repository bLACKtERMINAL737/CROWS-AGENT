"""Small cross-platform advisory locks for local files.

POSIX uses flock. Windows uses the CRT byte-range locking API. Lock files are
persistent coordination files and must not be deleted while processes may use
them.
"""
from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from typing import Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@contextmanager
def file_lock(fd: int, *, blocking: bool = True) -> Iterator[bool]:
    """Lock byte zero of an already-open file descriptor.

    Yields False only when a non-blocking acquisition is already held by
    another process. Blocking Windows acquisition retries until the lock is
    available, matching flock's blocking behavior.
    """
    if os.name == "nt":
        # msvcrt.locking locks a byte range, so make byte zero exist first.
        if os.fstat(fd).st_size == 0:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                os.write(fd, b"\0")
            except OSError:
                # Another process may have initialized the same lock file.
                if os.fstat(fd).st_size == 0:
                    raise
        retryable = {errno.EACCES, errno.EDEADLK}
        if hasattr(errno, "EDEADLOCK"):
            retryable.add(errno.EDEADLOCK)
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(
                    fd,
                    msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,
                    1,
                )
                break
            except OSError as exc:
                if exc.errno not in retryable:
                    raise
                if not blocking:
                    yield False
                    return
                time.sleep(0.05)
        try:
            yield True
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flags)
    except BlockingIOError:
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
