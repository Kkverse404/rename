from __future__ import annotations

# ruff: noqa: E402, I001

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from rename.daemon_lock import DaemonLock  # noqa: E402


with DaemonLock(sys.argv[1]):
    print("locked", flush=True)
    time.sleep(30)
