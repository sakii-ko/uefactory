from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from uefactory.core.config import Settings

doctor_app = typer.Typer(help="Check the local UEFactory runtime environment.")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    message: str
    details: dict[str, Any]


@doctor_app.callback(invoke_without_command=True)
def doctor(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON instead of a text table."),
    ] = False,
) -> None:
    settings = _settings_from_context(ctx)
    report = build_doctor_report(settings)
    if json_output:
        typer.echo(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_table(report)
    failed = any(check["status"] == "FAIL" for check in report["checks"])
    if failed:
        raise typer.Exit(1)


def build_doctor_report(settings: Settings) -> dict[str, Any]:
    start = time.monotonic()
    checks = [
        check_unreal_engine(settings),
        check_gpu(settings),
        check_vulkan(),
        check_disk(settings),
        check_python(),
    ]
    status = _overall_status(checks)
    report = {
        "schema_version": 1,
        "host": platform.node(),
        "status": status,
        "duration_sec": round(time.monotonic() - start, 3),
        "paths": {
            "project_root": str(settings.project_root),
            "ue_root": str(settings.ue_root),
            "data_dir": str(settings.data_dir),
            "log_dir": str(settings.log_dir),
            "ddc_dir": None if settings.ddc_dir is None else str(settings.ddc_dir),
        },
        "checks": [asdict(check) for check in checks],
    }
    LOGGER.debug("doctor_report=%s", report)
    return report


def check_unreal_engine(settings: Settings) -> CheckResult:
    executable = settings.ue_root / "Engine/Binaries/Linux/UnrealEditor-Cmd"
    build_version_path = settings.ue_root / "Engine/Build/Build.version"
    details: dict[str, Any] = {
        "ue_root": str(settings.ue_root),
        "executable": str(executable),
        "build_version_path": str(build_version_path),
    }
    if not executable.exists():
        return CheckResult("unreal_engine", "FAIL", "UnrealEditor-Cmd is missing", details)
    if not os.access(executable, os.X_OK):
        return CheckResult("unreal_engine", "FAIL", "UnrealEditor-Cmd is not executable", details)
    if not build_version_path.exists():
        return CheckResult("unreal_engine", "FAIL", "Build.version is missing", details)
    try:
        version = json.loads(build_version_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        details["error"] = str(exc)
        return CheckResult("unreal_engine", "FAIL", "Could not read Build.version", details)
    details["version"] = version
    version_text = "{MajorVersion}.{MinorVersion}.{PatchVersion}".format(**version)
    return CheckResult("unreal_engine", "OK", f"UE {version_text} found", details)


def check_gpu(settings: Settings) -> CheckResult:
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        return CheckResult(
            "gpu",
            "WARN",
            "nvidia-smi not found",
            {"min_free_vram_gib": settings.doctor.min_free_vram_gib},
        )
    query = "name,memory.total,memory.free,driver_version"
    try:
        result = subprocess.run(
            [
                nvidia_smi,
                f"--query-gpu={query}",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("gpu", "WARN", "nvidia-smi failed", {"error": str(exc)})
    details: dict[str, Any] = {
        "command": [nvidia_smi, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
        "returncode": result.returncode,
    }
    if result.returncode != 0:
        details["stderr"] = result.stderr.strip()
        return CheckResult("gpu", "WARN", "nvidia-smi returned non-zero", details)

    gpus = []
    low_memory = False
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        total_mib = _float_or_none(parts[1])
        free_mib = _float_or_none(parts[2])
        free_gib = None if free_mib is None else free_mib / 1024
        gpu = {
            "name": parts[0],
            "memory_total_mib": total_mib,
            "memory_free_mib": free_mib,
            "memory_free_gib": None if free_gib is None else round(free_gib, 2),
            "driver_version": parts[3],
        }
        if free_gib is not None and free_gib < settings.doctor.min_free_vram_gib:
            low_memory = True
        gpus.append(gpu)
    details["gpus"] = gpus
    if not gpus:
        return CheckResult("gpu", "WARN", "No GPUs reported by nvidia-smi", details)
    if low_memory:
        return CheckResult("gpu", "WARN", "At least one GPU has less than 8 GiB free VRAM", details)
    return CheckResult("gpu", "OK", f"{len(gpus)} GPU(s) available", details)


def check_vulkan() -> CheckResult:
    icd_path = Path("/etc/vulkan/icd.d/nvidia_icd.json")
    vulkaninfo = shutil.which("vulkaninfo")
    details: dict[str, Any] = {
        "nvidia_icd": str(icd_path),
        "nvidia_icd_exists": icd_path.exists(),
        "vulkaninfo": vulkaninfo,
    }
    if not icd_path.exists():
        return CheckResult("vulkan", "FAIL", "NVIDIA Vulkan ICD is missing", details)
    if vulkaninfo is None:
        return CheckResult(
            "vulkan", "WARN", "NVIDIA Vulkan ICD exists; vulkaninfo not installed", details
        )
    try:
        result = subprocess.run(
            [vulkaninfo, "--summary"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        details["error"] = str(exc)
        return CheckResult("vulkan", "WARN", "vulkaninfo failed", details)
    details["returncode"] = result.returncode
    details["summary_lines"] = result.stdout.strip().splitlines()[:80]
    if result.returncode != 0:
        details["stderr"] = result.stderr.strip()
        return CheckResult("vulkan", "WARN", "vulkaninfo returned non-zero", details)
    return CheckResult("vulkan", "OK", "Vulkan summary succeeded", details)


def check_disk(settings: Settings) -> CheckResult:
    candidates = [
        ("project_root", settings.project_root),
        ("data_dir", settings.data_dir),
    ]
    if settings.ddc_dir is not None:
        candidates.append(("ddc_dir", settings.ddc_dir))
    else:
        candidates.append(("default_ddc_dir", settings.data_dir / "ddc"))

    mounts = _mounts()
    local_mounts = [
        mount
        for mount in mounts
        if _is_candidate_local_mount(mount) and not _is_network_fs(mount["fstype"])
    ]
    network_mounts = [mount for mount in mounts if _is_network_fs(mount["fstype"])]
    paths = []
    warnings = []
    for label, path in candidates:
        path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(path)
        write_speed = _write_speed_mbps(path)
        mount = _mount_for_path(path, mounts)
        path_details = {
            "label": label,
            "path": str(path),
            "free_gib": round(usage.free / (1024**3), 2),
            "total_gib": round(usage.total / (1024**3), 2),
            "write_mbps": write_speed,
            "mount": mount,
        }
        paths.append(path_details)
        if write_speed is not None and write_speed < settings.doctor.nas_warn_write_mbps:
            warnings.append(
                f"{label} write speed is below {settings.doctor.nas_warn_write_mbps} MB/s"
            )
        if mount and _is_network_fs(str(mount.get("fstype"))):
            warnings.append(f"{label} is on network filesystem {mount.get('fstype')}")

    details: dict[str, Any] = {
        "paths": paths,
        "local_mounts": local_mounts,
        "network_mounts": network_mounts,
    }
    if warnings:
        details["warnings"] = warnings
        return CheckResult(
            "disk",
            "WARN",
            "Disk checks passed with storage warnings; DDC should prefer a local disk",
            details,
        )
    return CheckResult("disk", "OK", "Disk checks passed", details)


def check_python() -> CheckResult:
    details = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "platform": platform.platform(),
    }
    return CheckResult("python", "OK", f"Python {platform.python_version()}", details)


def _settings_from_context(ctx: typer.Context) -> Settings:
    obj = ctx.find_root().obj or {}
    settings = obj.get("settings")
    if not isinstance(settings, Settings):
        msg = "CLI settings were not initialized"
        raise RuntimeError(msg)
    return settings


def _print_table(report: dict[str, Any]) -> None:
    rows = [(check["name"], check["status"], check["message"]) for check in report["checks"]]
    widths = [
        max(len("CHECK"), *(len(row[0]) for row in rows)),
        max(len("STATUS"), *(len(row[1]) for row in rows)),
        max(len("MESSAGE"), *(len(row[2]) for row in rows)),
    ]
    header = f"{'CHECK':<{widths[0]}}  {'STATUS':<{widths[1]}}  {'MESSAGE':<{widths[2]}}"
    typer.echo(header)
    typer.echo(f"{'-' * widths[0]}  {'-' * widths[1]}  {'-' * widths[2]}")
    for name, status, message in rows:
        typer.echo(f"{name:<{widths[0]}}  {status:<{widths[1]}}  {message:<{widths[2]}}")
    typer.echo(f"\nOverall: {report['status']}")


def _overall_status(checks: list[CheckResult]) -> str:
    if any(check.status == "FAIL" for check in checks):
        return "FAIL"
    if any(check.status == "WARN" for check in checks):
        return "WARN"
    return "OK"


def _float_or_none(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _write_speed_mbps(path: Path) -> float | None:
    size_mib = int(os.environ.get("UEF_DOCTOR_WRITE_TEST_MIB", "512"))
    if size_mib <= 0:
        return None
    path.mkdir(parents=True, exist_ok=True)
    fd = -1
    tmp_path = Path()
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
            return None
        return round(size_mib / duration, 2)
    except OSError as exc:
        LOGGER.warning("write speed test failed for %s: %s", path, exc)
        return None
    finally:
        if fd != -1:
            os.close(fd)
        if tmp_path:
            tmp_path.unlink(missing_ok=True)


def _mounts() -> list[dict[str, Any]]:
    mounts: list[dict[str, Any]] = []
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return mounts
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mounts.append(
            {
                "source": parts[0],
                "mountpoint": parts[1].replace("\\040", " "),
                "fstype": parts[2],
            }
        )
    return mounts


def _mount_for_path(path: Path, mounts: list[dict[str, Any]]) -> dict[str, Any] | None:
    resolved = path.resolve()
    best: dict[str, Any] | None = None
    best_len = -1
    for mount in mounts:
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


def _is_network_fs(fstype: str) -> bool:
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


def _is_candidate_local_mount(mount: dict[str, Any]) -> bool:
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
