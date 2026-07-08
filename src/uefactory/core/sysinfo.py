from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WriteSpeedResult:
    mbps: float | None
    size_mib: int
    error: str | None = None
    skipped_reason: str | None = None


def write_speed_mbps(path: Path, size_mib: int) -> WriteSpeedResult:
    if size_mib <= 0:
        return WriteSpeedResult(
            mbps=None,
            size_mib=size_mib,
            skipped_reason="write test disabled",
        )
    path.mkdir(parents=True, exist_ok=True)
    fd = -1
    tmp_path: Path | None = None
    try:
        fd, raw_tmp_path = tempfile.mkstemp(prefix=".uef_write_", dir=path)
        tmp_path = Path(raw_tmp_path)
        block = b"\0" * (1024 * 1024)
        start = time.monotonic()
        with os.fdopen(fd, "wb", closefd=True) as file:
            fd = -1
            for _ in range(size_mib):
                file.write(block)
            file.flush()
            os.fsync(file.fileno())
        duration = time.monotonic() - start
        if duration <= 0:
            return WriteSpeedResult(
                mbps=None,
                size_mib=size_mib,
                skipped_reason="duration was not positive",
            )
        return WriteSpeedResult(mbps=round(size_mib / duration, 2), size_mib=size_mib)
    except OSError as exc:
        LOGGER.warning("write speed test failed for %s: %s", path, exc)
        return WriteSpeedResult(mbps=None, size_mib=size_mib, error=str(exc))
    finally:
        if fd != -1:
            os.close(fd)
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def mounts() -> list[dict[str, Any]]:
    parsed_mounts: list[dict[str, Any]] = []
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return parsed_mounts
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        parsed_mounts.append(
            {
                "source": parts[0],
                "mountpoint": parts[1].replace("\\040", " "),
                "fstype": parts[2],
            }
        )
    return parsed_mounts


def mount_for_path(path: Path, mount_table: list[dict[str, Any]]) -> dict[str, Any] | None:
    resolved = path.resolve()
    best: dict[str, Any] | None = None
    best_len = -1
    for mount in mount_table:
        mountpoint = Path(str(mount["mountpoint"]))
        try:
            resolved.relative_to(mountpoint)
        except ValueError:
            continue
        length = len(str(mountpoint))
        if length > best_len:
            best = mount
            best_len = length
    return best


def is_network_fs(fstype: str) -> bool:
    return fstype.lower() in {
        "ceph",
        "cifs",
        "dingofs",
        "fuse.ceph",
        "fuse.dingofs",
        "fuse.sshfs",
        "gpfs",
        "nfs",
        "nfs4",
        "smb3",
    }


def is_candidate_local_mount(mount: dict[str, Any]) -> bool:
    fstype = str(mount["fstype"]).lower()
    mountpoint = Path(str(mount["mountpoint"]))
    ignored_fstypes = {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "overlay",
        "proc",
        "pstore",
        "rpc_pipefs",
        "securityfs",
        "sysfs",
        "tmpfs",
        "tracefs",
    }
    if fstype in ignored_fstypes:
        return False
    if fstype.startswith("fuse."):
        return False
    if not mountpoint.exists() or not mountpoint.is_dir():
        return False
    return str(mountpoint) in {"/", "/scratch", "/tmp"} or not any(
        str(mountpoint).startswith(prefix)
        for prefix in (
            "/proc",
            "/sys",
            "/dev",
            "/run",
            "/usr/bin",
            "/usr/lib",
            "/usr/share",
            "/etc",
        )
    )
