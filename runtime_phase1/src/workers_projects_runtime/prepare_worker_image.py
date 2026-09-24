"""Prepare the reviewed GlassHive workstation image during runtime startup."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True

from .docker_sandbox import DockerSandboxManager


def main() -> int:
    raw_db_path = str(os.environ.get("WPR_DB_PATH") or "").strip()
    if not raw_db_path:
        raise RuntimeError("WPR_DB_PATH is required to prepare the worker image")
    db_path = Path(raw_db_path).expanduser()
    if not db_path.is_absolute():
        raise RuntimeError("WPR_DB_PATH must be absolute")
    DockerSandboxManager(base_dir=str(db_path.resolve().parent)).prepare_image()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
