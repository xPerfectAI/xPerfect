"""Read Linux memory headroom, bounded by visible cgroup memory limits."""
from pathlib import Path, PurePosixPath


def _mount_path(value: str) -> str:
    for encoded, plain in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
        value = value.replace(encoded, plain)
    return value


def available_memory_bytes(proc: Path = Path("/proc")) -> int:
    fields = dict(line.split(":", 1) for line in (proc / "meminfo").read_text().splitlines() if ":" in line)
    amount, unit = fields["MemAvailable"].split()
    if unit != "kB" or not amount.isdecimal():
        raise ValueError("Invalid Linux available-memory measurement")
    available = int(amount) * 1024
    memberships = [line.split(":", 2) for line in (proc / "self/cgroup").read_text().splitlines()]
    unified = next((path for _, controllers, path in memberships if controllers == ""), None)
    legacy = next((path for _, controllers, path in memberships if "memory" in controllers.split(",")), None)
    membership = legacy if legacy is not None else unified
    if membership is None:
        return available  # Kernel with no memory cgroup controller.
    expected_fs = "cgroup" if legacy is not None else "cgroup2"
    for line in (proc / "self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        details, mount = after.split(), before.split()
        if details[0] != expected_fs or (legacy is not None and "memory" not in details[2].split(",")):
            continue
        mount_root, mountpoint = PurePosixPath(_mount_path(mount[3])), Path(_mount_path(mount[4]))
        try:
            relative = PurePosixPath(membership).relative_to(mount_root)
        except ValueError:
            continue
        if ".." in relative.parts:
            raise ValueError("Invalid memory cgroup membership")
        current = mountpoint.joinpath(*relative.parts)
        while True:
            limit_path = current / ("memory.limit_in_bytes" if legacy is not None else "memory.max")
            usage_path = current / ("memory.usage_in_bytes" if legacy is not None else "memory.current")
            if limit_path.exists():
                limit = limit_path.read_text().strip()
                if limit != "max":
                    used = usage_path.read_text().strip()
                    if not limit.isdecimal() or not used.isdecimal():
                        raise ValueError("Invalid memory cgroup measurement")
                    available = min(available, max(0, int(limit) - int(used)))
            elif current != mountpoint:
                raise ValueError("Memory cgroup limit is unavailable")
            if current == mountpoint:
                return available
            current = current.parent
    raise ValueError("Memory cgroup mount is unavailable")
