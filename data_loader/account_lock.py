"""One acquisition run per source account at a time, enforced by `run`.

USGS M2M allows one download session per account: two runs on one account
(even on different datasets) make each other fail with rate-limit errors.
A provider whose source has such a limit says so with
`account_lock_key()`; the engine then holds an exclusive lock named after
that key for the whole run.

The lock file lives in $DATALOADER_LOCK_DIR, default
~/.cache/data-loader/locks (one machine, one user). Point every
maintainer's DATALOADER_LOCK_DIR at one shared directory (the imagery
library uses <library>/admin/locks) to enforce it across users and
machines: the lock is flock(2), which NFSv4 holds on the server.

The key never contains the account name (it is a hash), and the lock
file records only pid, host, start time and output directory.
"""
from __future__ import annotations

import fcntl
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

ENV = "DATALOADER_LOCK_DIR"
DEFAULT_DIR = Path.home() / ".cache" / "data-loader" / "locks"
WAIT_LOG_EVERY_S = 600
POLL_S = 5


class AccountBusy(RuntimeError):
    """Another run holds the source account; nothing was done."""


def lock_dir() -> Path:
    return Path(os.environ.get(ENV) or DEFAULT_DIR)


class AccountLock:
    def __init__(self, key: str, output_dir: str):
        self.path = lock_dir() / f"{key}.lock"
        self.output_dir = output_dir
        self.fd: Optional[int] = None

    def _holder(self) -> str:
        try:
            return self.path.read_text().strip() or "unknown"
        except OSError:
            return "unknown"

    def acquire(self, wait: bool, log: Callable[[str], None] = print) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o664)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not wait:
                os.close(fd)
                raise AccountBusy(
                    f"another run is using this source account (lock {self.path}: {self._holder()}). "
                    "Only one run per account may run at a time; wait for it to finish, or pass "
                    "--wait-for-account to queue behind it") from None
            log(f"[run] waiting for the source account (held by: {self._holder()})")
            t0 = time.monotonic()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(POLL_S)
                    if int(time.monotonic() - t0) % WAIT_LOG_EVERY_S < POLL_S:
                        log(f"[run] still waiting for the source account ({int(time.monotonic() - t0) // 60} min)")
        os.ftruncate(fd, 0)
        os.write(fd, (f"pid {os.getpid()} on {os.uname().nodename} since "
                      f"{datetime.now(timezone.utc).replace(microsecond=0).isoformat()} -> {self.output_dir}\n").encode())
        os.fsync(fd)
        self.fd = fd

    def release(self) -> None:
        if self.fd is not None:
            try:
                os.ftruncate(self.fd, 0)
            except OSError:  # pragma: no cover
                pass
            os.close(self.fd)
            self.fd = None


def account_lock_for(provider, output_dir) -> Optional[AccountLock]:
    """The lock a run with this provider must hold, or None if its source
    has no per-account limit."""
    key_fn = getattr(provider, "account_lock_key", None)
    key = key_fn() if callable(key_fn) else None
    return AccountLock(key, str(output_dir)) if key else None
