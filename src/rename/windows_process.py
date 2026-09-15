"""Cross-platform subprocess options for background helpers."""

import os

CREATE_NO_WINDOW = 0x08000000


def background_creationflags() -> int:
    """Prevent console-subsystem children from flashing a window on Windows."""

    return CREATE_NO_WINDOW if os.name == "nt" else 0


__all__ = ["CREATE_NO_WINDOW", "background_creationflags"]
