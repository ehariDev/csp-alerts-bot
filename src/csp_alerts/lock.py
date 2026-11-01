"""Single-instance lock for scheduled jobs (OS-level file lock, released automatically if the process dies).

Windows uses msvcrt.locking, other platforms fcntl.flock. A second job that cannot take the lock exits with
code 3 and does nothing (a skipped 15-minute monitor is harmless; the next one runs).
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_instance(path: Path | str):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "a+b")
    try:
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning(f"another csp-alerts job holds {p}") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()}\n".encode())
        fh.flush()
        try:
            yield
        finally:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fh.close()
