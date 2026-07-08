from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

from uefactory.cli.doctor import CheckResult, build_doctor_report, check_disk, check_gpu
from uefactory.core.config import DoctorConfig, Settings
from uefactory.core.sysinfo import write_speed_mbps


def test_doctor_json_schema(monkeypatch: Any, tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path,
        ue_root=tmp_path / "engine",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        ddc_dir=tmp_path / "ddc",
    )

    monkeypatch.setattr(
        "uefactory.cli.doctor.check_unreal_engine",
        lambda settings: CheckResult("unreal_engine", "OK", "engine ok", {"ue_root": "x"}),
    )
    monkeypatch.setattr(
        "uefactory.cli.doctor.check_gpu",
        lambda settings: CheckResult("gpu", "WARN", "gpu warn", {"gpus": []}),
    )
    monkeypatch.setattr(
        "uefactory.cli.doctor.check_vulkan",
        lambda settings: CheckResult("vulkan", "OK", "vulkan ok", {}),
    )
    monkeypatch.setattr(
        "uefactory.cli.doctor.check_disk",
        lambda settings: CheckResult("disk", "OK", "disk ok", {}),
    )
    monkeypatch.setattr(
        "uefactory.cli.doctor.check_python",
        lambda: CheckResult("python", "OK", "python ok", {}),
    )

    report = build_doctor_report(settings)

    assert report["schema_version"] == 1
    assert report["status"] == "WARN"
    assert isinstance(report["duration_sec"], float)
    assert report["paths"]["project_root"] == str(tmp_path)
    assert report["paths"]["ue_home"] == str(settings.ue_home)
    assert report["paths"]["runtime_lib_dir"] is None
    assert [check["name"] for check in report["checks"]] == [
        "unreal_engine",
        "gpu",
        "vulkan",
        "disk",
        "python",
    ]
    assert all({"name", "status", "message", "details"} <= set(check) for check in report["checks"])


def test_write_speed_reports_mkstemp_failure_without_unlinking_cwd(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    def fail_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        raise PermissionError("read-only candidate")

    monkeypatch.setattr("tempfile.mkstemp", fail_mkstemp)

    result = write_speed_mbps(tmp_path, 1)

    assert result.mbps is None
    assert result.error is not None
    assert "read-only candidate" in result.error


def test_check_disk_fails_when_write_test_fails(monkeypatch: Any, tmp_path: Path) -> None:
    def fail_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        raise PermissionError("read-only candidate")

    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        ddc_dir=tmp_path / "ddc",
        doctor=DoctorConfig(write_test_mib=1),
    )
    monkeypatch.setattr("tempfile.mkstemp", fail_mkstemp)
    monkeypatch.setattr("uefactory.cli.doctor.mounts", lambda: [])

    result = check_disk(settings)

    assert result.status == "FAIL"
    assert result.details["failures"]


def test_gpu_warning_uses_configured_vram_threshold(monkeypatch: Any, tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        doctor=DoctorConfig(min_free_vram_gib=12),
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, "GPU, 24576, 8192, 580.0\n", ""),
    )

    result = check_gpu(settings)

    assert result.status == "WARN"
    assert "12 GiB" in result.message
