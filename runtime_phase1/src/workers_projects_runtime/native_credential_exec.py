"""Read declared native API-key artifacts only after UID and quota-guard entry."""
import json
import os
from pathlib import Path
import stat
import sys


def credential_environment(selectors: dict[str, str]) -> dict[str, str]:
    if not isinstance(selectors, dict) or set(selectors) - {'ANTHROPIC_API_KEY', 'XAI_API_KEY'}:
        raise ValueError('Unsupported native credential selector')
    values = {}
    for key, raw in selectors.items():
        path = Path(raw)
        home = Path(os.environ['HOME'])
        if not path.is_absolute() or '..' in path.parts or home not in path.parents:
            raise ValueError('Credential is outside the private native home')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r') as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_mode & 0o007 or metadata.st_size > 16384):
                raise ValueError('Invalid private credential artifact')
            value = json.loads(stream.read(16385))['value']
            if (not isinstance(value, str) or not 1 <= len(value) <= 8192
                    or any(character.isspace() or ord(character) < 33 or ord(character) == 127 for character in value)):
                raise ValueError('Invalid native credential')
            values[key] = value
    return values


def main():
    try:
        if len(sys.argv) < 4 or sys.argv[2] != '--':
            raise ValueError('Invalid native credential launch')
        environment = {**os.environ, **credential_environment(json.loads(sys.argv[1]))}
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit('Native credential projection is unavailable') from None
    os.execvpe(sys.argv[3], sys.argv[3:], environment)


if __name__ == '__main__':
    main()
