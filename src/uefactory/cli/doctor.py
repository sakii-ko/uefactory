from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from uefactory.cli._common import settings_from_context
from uefactory.core.config import Settings
from uefactory.core.remote_probe import build_remote_doctor_report
from uefactory.core.sysinfo import (
    is_candidate_local_mount,
    is_network_fs,
    mount_for_path,
    mounts,
    write_speed_mbps,
)

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
    host: Annotated[
        str | None,
        typer.Option("--host", help="Run doctor checks on a configured remote host."),
    ] = None,
) -> None:
    settings = settings_from_context(ctx)
    report = (
        build_doctor_report(settings)
        if host is None
        else build_remote_doctor_report(settings, host)
    )
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
        check_vulkan(settings),
        check_disk(settings),
        check_ffmpeg(),
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
            "ue_home": str(settings.ue_home),
            "data_dir": str(settings.data_dir),
            "log_dir": str(settings.log_dir),
            "ddc_dir": None if settings.ddc_dir is None else str(settings.ddc_dir),
            "runtime_lib_dir": (
                None if settings.runtime_lib_dir is None else str(settings.runtime_lib_dir)
            ),
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
        threshold = settings.doctor.min_free_vram_gib
        return CheckResult(
            "gpu",
            "WARN",
            f"At least one GPU has less than {threshold:g} GiB free VRAM",
            details,
        )
    return CheckResult("gpu", "OK", f"{len(gpus)} GPU(s) available", details)


def check_vulkan(settings: Settings) -> CheckResult:
    icd_path = Path("/etc/vulkan/icd.d/nvidia_icd.json")
    vulkaninfo = shutil.which("vulkaninfo")
    configured_loader = _configured_vulkan_loader(settings)
    system_loader = _system_library_path("libvulkan.so.1")
    details: dict[str, Any] = {
        "nvidia_icd": str(icd_path),
        "nvidia_icd_exists": icd_path.exists(),
        "vulkaninfo": vulkaninfo,
        "system_libvulkan": system_loader,
        "configured_libvulkan": None if configured_loader is None else str(configured_loader),
    }
    if not icd_path.exists():
        return CheckResult("vulkan", "FAIL", "NVIDIA Vulkan ICD is missing", details)
    if system_loader is None and configured_loader is None:
        return CheckResult(
            "vulkan",
            "WARN",
            "NVIDIA Vulkan ICD exists, but libvulkan.so.1 was not found",
            details,
        )
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


def _configured_vulkan_loader(settings: Settings) -> Path | None:
    if settings.runtime_lib_dir is None:
        return None
    loader = settings.runtime_lib_dir / "libvulkan.so.1"
    return loader if loader.exists() else None


def _system_library_path(library_name: str) -> str | None:
    try:
        result = subprocess.run(
            ["ldconfig", "-p"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    marker = f"{library_name} "
    for line in result.stdout.splitlines():
        if marker in line and "=>" in line:
            return line.rsplit("=>", maxsplit=1)[1].strip()
    return None


def check_disk(settings: Settings) -> CheckResult:
    candidates = [
        ("project_root", settings.project_root),
        ("data_dir", settings.data_dir),
    ]
    if settings.ddc_dir is not None:
        candidates.append(("ddc_dir", settings.ddc_dir))
    else:
        candidates.append(("default_ddc_dir", settings.data_dir / "ddc"))

    mount_table = mounts()
    local_mounts = [
        mount
        for mount in mount_table
        if is_candidate_local_mount(mount) and not is_network_fs(mount["fstype"])
    ]
    network_mounts = [mount for mount in mount_table if is_network_fs(mount["fstype"])]
    paths = []
    warnings = []
    failures = []
    for label, path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(path)
        except OSError as exc:
            path_details: dict[str, Any] = {
                "label": label,
                "path": str(path),
                "error": str(exc),
            }
            paths.append(path_details)
            failures.append(f"{label} path is not usable: {exc}")
            continue

        write_speed = write_speed_mbps(path, settings.doctor.write_test_mib)
        mount = mount_for_path(path, mount_table)
        path_details = {
            "label": label,
            "path": str(path),
            "free_gib": round(usage.free / (1024**3), 2),
            "total_gib": round(usage.total / (1024**3), 2),
            "write_mbps": write_speed.mbps,
            "write_test": asdict(write_speed),
            "mount": mount,
        }
        paths.append(path_details)
        if write_speed.error is not None:
            failures.append(f"{label} write test failed: {write_speed.error}")
        elif (
            write_speed.mbps is not None and write_speed.mbps < settings.doctor.nas_warn_write_mbps
        ):
            warnings.append(
                f"{label} write speed is below {settings.doctor.nas_warn_write_mbps} MB/s"
            )
        if mount and is_network_fs(str(mount.get("fstype"))):
            warnings.append(f"{label} is on network filesystem {mount.get('fstype')}")

    details: dict[str, Any] = {
        "paths": paths,
        "local_mounts": local_mounts,
        "network_mounts": network_mounts,
    }
    if failures:
        details["failures"] = failures
        if warnings:
            details["warnings"] = warnings
        return CheckResult("disk", "FAIL", "Disk write checks failed", details)
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


def check_ffmpeg() -> CheckResult:
    executable = shutil.which("ffmpeg")
    details: dict[str, Any] = {"executable": executable}
    if executable is None:
        return CheckResult("ffmpeg", "WARN", "ffmpeg not found; turntable videos disabled", details)
    try:
        result = subprocess.run(
            [executable, "-version"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        details["error"] = str(exc)
        return CheckResult("ffmpeg", "WARN", "ffmpeg failed", details)
    details["returncode"] = result.returncode
    details["version_line"] = result.stdout.splitlines()[0] if result.stdout else ""
    if result.returncode != 0:
        details["stderr"] = result.stderr.strip()
        return CheckResult("ffmpeg", "WARN", "ffmpeg returned non-zero", details)
    return CheckResult("ffmpeg", "OK", details["version_line"], details)


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
