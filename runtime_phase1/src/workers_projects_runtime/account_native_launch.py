"""Trusted native setup launch seam; presence alone is not guard acceptance evidence."""
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
from typing import Any, Literal, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .contained_account_launch import ContainedAccountProcess

AccountLaunchPurpose = Literal['setup', 'verify', 'logout', 'api-key-login', 'api-key-verify']


@dataclass(frozen=True)
class NativeAccountLaunch:
    account_home: Path
    command: tuple[str, ...]
    environment: dict[str, str] = field(repr=False)
    purpose: AccountLaunchPurpose = 'verify'
    lease_id: str = ''


class GuardedAccountLauncher(Protocol):
    """Launch native code after UID/capability drop and immutable quota guard entry.

    The implementation owns exact container generation and all-descendant lifetime.
    Return a ContainedAccountProcess, never a Docker CLI proxy as a native PID.
    Persist pending identity before creating native containment. Never persist environment secrets in Docker metadata/argv.
    Blocking run must reap/stop the whole native container on timeout before returning or
    raising. Restore trusted ownership only after exact stop, before account sealing.
    """
    def assert_quiescent(self, account_home: Path) -> None: ...
    def popen(self, request: NativeAccountLaunch, **options: Any) -> "ContainedAccountProcess": ...
    def run(self, request: NativeAccountLaunch, **options: Any) -> subprocess.CompletedProcess[Any]: ...
