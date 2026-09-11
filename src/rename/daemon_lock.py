"""One process-wide daemon lease with no persistent Windows state."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import BinaryIO


class DaemonAlreadyRunningError(RuntimeError):
    """Another daemon owns the same state-directory lease."""


class DaemonLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: int | None = None
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None or self._file is not None:
            return
        if os.name == "nt":
            self._acquire_windows()
        else:
            self._acquire_posix()

    def _acquire_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        digest = hashlib.sha256(str(self.path.resolve()).casefold().encode("utf-8")).hexdigest()
        name = f"Local\\rename-daemon-{digest[:24]}"
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        create_mutex.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = create_mutex(None, False, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            close_handle(handle)
            raise DaemonAlreadyRunningError("rename daemon is already running")
        self._handle = int(handle)

    def _acquire_posix(self) -> None:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(self.path, "a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise DaemonAlreadyRunningError("rename daemon is already running") from exc
        self._file = lock_file

    def release(self) -> None:
        if self._handle is not None:
            import ctypes
            from ctypes import wintypes

            close_handle = ctypes.WinDLL(
                "kernel32", use_last_error=True
            ).CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            close_handle(self._handle)
            self._handle = None
        if self._file is not None:
            import fcntl

            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                self._file.close()
                self._file = None

    def __enter__(self) -> "DaemonLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


__all__ = ["DaemonAlreadyRunningError", "DaemonLock"]
