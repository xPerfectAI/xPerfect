"""Immutable image entry after quota seccomp installation; no controller authority."""
import json
import os
from pathlib import Path
import stat
import sys


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Account launch payload is required')
    path = Path(sys.argv[1])
    if path.parent != Path('/workspace/account') or not path.name.startswith('.xperfect-launch-'):
        raise SystemExit('Invalid account launch payload')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise SystemExit('Unsafe account launch payload')
        payload = json.loads(handle.read(131073))
    command, environment = payload['command'], payload['environment']
    if (not isinstance(command, list) or not command or not all(isinstance(v, str) and '\0' not in v for v in command)
            or not Path(command[0]).is_absolute() or not isinstance(environment, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) or '\0' in k+v
                   or k.startswith('LD_') or k in {'PYTHONHOME','PYTHONPATH'} for k,v in environment.items())):
        raise SystemExit('Invalid account launch inputs')
    path.unlink()
    os.chdir('/workspace/account')
    os.execve(command[0], command, environment)


if __name__ == '__main__':
    main()
