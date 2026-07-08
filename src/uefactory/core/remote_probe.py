from __future__ import annotations

import logging
import uuid
from typing import Any

from uefactory.core.config import Settings
from uefactory.core.remote import RemoteHost, parse_json_stdout, remote_python_command

LOGGER = logging.getLogger(__name__)


def build_remote_doctor_report(settings: Settings, host: str) -> dict[str, Any]:
    remote = RemoteHost.from_settings(settings, host)
    command = remote_python_command(
        REMOTE_DOCTOR_SCRIPT,
        {
            "UEF_HOST_NAME": remote.config.name,
            "UEF_SSH_ALIAS": remote.config.ssh_alias,
            "UEF_WORK_DIR": str(remote.config.work_dir),
            "UEF_ENGINE_DIR": str(remote.config.engine_dir),
            "UEF_GPU": remote.config.gpu or "",
            "UEF_PROBE_ID": uuid.uuid4().hex,
        },
    )
    result = remote.run(command, timeout_sec=60)
    report = parse_json_stdout(result.stdout)
    report["transport"] = {
        "ssh_alias": remote.config.ssh_alias,
        "ssh_connection_count": 1,
        "command_duration_sec": result.duration_sec,
        "control_master": True,
    }
    report["status"] = _overall_status(report["checks"])
    LOGGER.debug("remote_doctor_report=%s", report)
    return report


def _overall_status(checks: list[dict[str, Any]]) -> str:
    if any(check["status"] == "FAIL" for check in checks):
        return "FAIL"
    if any(check["status"] == "WARN" for check in checks):
        return "WARN"
    return "OK"


REMOTE_DOCTOR_SCRIPT = r"""
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path


def check(name, status, message, details):
    return {"name": name, "status": status, "message": message, "details": details}


def run(argv, timeout):
    started = time.monotonic()
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, round(time.monotonic() - started, 3), exc
    return result, round(time.monotonic() - started, 3), None


def parse_mounts():
    mounts = []
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return mounts
    for line in lines:
        parts = line.split()
        if len(parts) >= 3:
            mounts.append(
                {
                    "source": parts[0],
                    "mountpoint": parts[1].replace("\\040", " "),
                    "fstype": parts[2],
                }
            )
    return mounts


def mount_for(path, mount_table):
    resolved = Path(path).resolve()
    best = None
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


def is_network_fs(fstype):
    return str(fstype).lower() in {
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


def system_library_path(library_name):
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


started = time.monotonic()
host_name = os.environ["UEF_HOST_NAME"]
work_dir = Path(os.environ["UEF_WORK_DIR"])
engine_dir = Path(os.environ["UEF_ENGINE_DIR"])
checks = []

sentinel = work_dir / ".uef_node"
sentinel_details = {"work_dir": str(work_dir), "sentinel": str(sentinel)}
if sentinel.exists():
    try:
        payload = json.loads(sentinel.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sentinel_details["error"] = str(exc)
        checks.append(
            check("node_sentinel", "FAIL", "Node sentinel is unreadable", sentinel_details)
        )
    else:
        sentinel_details["payload"] = payload
        if payload.get("host") == host_name:
            checks.append(
                check("node_sentinel", "OK", "Node sentinel matches host", sentinel_details)
            )
        else:
            checks.append(
                check("node_sentinel", "FAIL", "Node sentinel host mismatch", sentinel_details)
            )
else:
    checks.append(
        check(
            "node_sentinel",
            "FAIL",
            "Node sentinel is missing; run uef node init",
            sentinel_details,
        )
    )

engine_version_path = engine_dir / "Engine/Build/Build.version"
engine_executable = engine_dir / "Engine/Binaries/Linux/UnrealEditor-Cmd"
engine_details = {
    "engine_dir": str(engine_dir),
    "executable": str(engine_executable),
    "build_version_path": str(engine_version_path),
    "exists": engine_version_path.exists(),
}
if engine_version_path.exists():
    try:
        engine_details["version"] = json.loads(engine_version_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        engine_details["error"] = str(exc)
        checks.append(
            check("unreal_engine", "WARN", "Remote engine version is unreadable", engine_details)
        )
    else:
        checks.append(check("unreal_engine", "OK", "Remote UE install found", engine_details))
else:
    checks.append(
        check("unreal_engine", "WARN", "Remote UE install not provisioned", engine_details)
    )

nvidia_smi = shutil.which("nvidia-smi")
gpu_details = {"expected_gpu": os.environ.get("UEF_GPU") or None, "nvidia_smi": nvidia_smi}
if nvidia_smi is None:
    checks.append(check("gpu", "WARN", "nvidia-smi not found", gpu_details))
else:
    result, duration, error = run(
        [
            nvidia_smi,
            "--query-gpu=name,memory.total,memory.free,driver_version",
            "--format=csv,noheader,nounits",
        ],
        15,
    )
    gpu_details["duration_sec"] = duration
    if error is not None:
        gpu_details["error"] = str(error)
        checks.append(check("gpu", "WARN", "nvidia-smi failed", gpu_details))
    elif result is not None:
        gpu_details["returncode"] = result.returncode
        if result.returncode == 0:
            gpus = []
            for line in result.stdout.splitlines():
                parts = [part.strip() for part in line.split(",")]
                if len(parts) >= 4:
                    free_mib = float(parts[2])
                    gpus.append({
                        "name": parts[0],
                        "memory_total_mib": float(parts[1]),
                        "memory_free_mib": free_mib,
                        "memory_free_gib": round(free_mib / 1024, 2),
                        "driver_version": parts[3],
                    })
            gpu_details["gpus"] = gpus
            checks.append(
                check(
                    "gpu",
                    "OK" if gpus else "WARN",
                    f"{len(gpus)} GPU(s) reported",
                    gpu_details,
                )
            )
        else:
            gpu_details["stderr"] = result.stderr.strip()
            checks.append(check("gpu", "WARN", "nvidia-smi returned non-zero", gpu_details))

icd_path = Path("/etc/vulkan/icd.d/nvidia_icd.json")
vulkaninfo = shutil.which("vulkaninfo")
vulkan_details = {
    "nvidia_icd": str(icd_path),
    "nvidia_icd_exists": icd_path.exists(),
    "vulkaninfo": vulkaninfo,
    "system_libvulkan": system_library_path("libvulkan.so.1"),
}
if not icd_path.exists():
    checks.append(check("vulkan", "FAIL", "NVIDIA Vulkan ICD is missing", vulkan_details))
elif vulkaninfo is None:
    checks.append(
        check(
            "vulkan",
            "WARN",
            "NVIDIA Vulkan ICD exists; vulkaninfo not installed",
            vulkan_details,
        )
    )
else:
    result, duration, error = run([vulkaninfo, "--summary"], 30)
    vulkan_details["duration_sec"] = duration
    if error is not None:
        vulkan_details["error"] = str(error)
        checks.append(check("vulkan", "WARN", "vulkaninfo failed", vulkan_details))
    elif result is not None:
        vulkan_details["returncode"] = result.returncode
        vulkan_details["summary_lines"] = result.stdout.strip().splitlines()[:80]
        if result.returncode == 0:
            checks.append(check("vulkan", "OK", "Vulkan summary succeeded", vulkan_details))
        else:
            vulkan_details["stderr"] = result.stderr.strip()
            checks.append(check("vulkan", "WARN", "vulkaninfo returned non-zero", vulkan_details))

mount_table = parse_mounts()
disk_paths = [("work_dir", work_dir), ("engine_dir", engine_dir.parent)]
disk_details = {
    "paths": [],
    "network_mounts": [m for m in mount_table if is_network_fs(m["fstype"])],
}
disk_warnings = []
disk_failures = []
for label, path in disk_paths:
    try:
        path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(path)
    except OSError as exc:
        disk_failures.append(f"{label} is not usable: {exc}")
        disk_details["paths"].append({"label": label, "path": str(path), "error": str(exc)})
        continue
    mount = mount_for(path, mount_table)
    entry = {
        "label": label,
        "path": str(path),
        "free_gib": round(usage.free / (1024 ** 3), 2),
        "total_gib": round(usage.total / (1024 ** 3), 2),
        "mount": mount,
    }
    disk_details["paths"].append(entry)
    if mount and is_network_fs(mount.get("fstype")):
        disk_warnings.append(f"{label} is on network filesystem {mount.get('fstype')}")
    if host_name == "4090" and label == "work_dir" and usage.free < 150 * (1024 ** 3):
        disk_warnings.append("4090 work_dir has less than 150 GiB free")
if host_name == "4090":
    disk_warnings.append("4090 is shared and storage-constrained; clean remote temp after each job")
if host_name == "l40s":
    disk_warnings.append("l40s /root/nas/bigdata1 is a different filesystem from local NAS")
if disk_failures:
    disk_details["failures"] = disk_failures
    checks.append(check("disk", "FAIL", "Remote disk checks failed", disk_details))
elif disk_warnings:
    disk_details["warnings"] = disk_warnings
    checks.append(check("disk", "WARN", "Remote disk checks passed with warnings", disk_details))
else:
    checks.append(check("disk", "OK", "Remote disk checks passed", disk_details))

python_details = {
    "python": platform.python_version(),
    "implementation": platform.python_implementation(),
    "executable": sys.executable if "sys" in globals() else None,
    "platform": platform.platform(),
}
checks.append(check("python", "OK", f"Python {platform.python_version()}", python_details))

status = "OK"
if any(item["status"] == "FAIL" for item in checks):
    status = "FAIL"
elif any(item["status"] == "WARN" for item in checks):
    status = "WARN"

report = {
    "schema_version": 1,
    "host": platform.node(),
    "remote_host": host_name,
    "status": status,
    "duration_sec": round(time.monotonic() - started, 3),
    "paths": {
        "work_dir": str(work_dir),
        "engine_dir": str(engine_dir),
    },
    "probe_id": os.environ["UEF_PROBE_ID"],
    "checks": checks,
}
print(json.dumps(report, sort_keys=True))
"""
