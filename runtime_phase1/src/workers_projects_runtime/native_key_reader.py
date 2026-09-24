"""Small native-container entrypoint for one selected mounted API key.

Keep this module standard-library-only: the guarded native image intentionally
does not contain the runtime control plane or its dependencies.
"""
from __future__ import annotations

import json
import os
import stat
import sys


class NativeKeyReadError(ValueError):
    pass


def validate_key(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(
        not 33 <= ord(char) <= 126 for char in value
    ):
        raise NativeKeyReadError("Enter a non-empty API key without spaces or control characters")
    return value


def key_value(path: str, *, expected_mount: str = "/workspace/.provider-account") -> str:
    if path != f"{expected_mount}/api-key.json":
        raise NativeKeyReadError("Native API key mount is invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o007:
                raise ValueError("unsafe file")
            payload = json.loads(stream.read(16385))
            return validate_key(payload["value"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise NativeKeyReadError("Private API key is missing or unsafe; reconnect this account") from exc


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        sys.stdout.write(key_value(sys.argv[1]))
    except NativeKeyReadError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
