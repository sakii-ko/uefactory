from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from typing import Any

from uefactory.cli.doctor import CheckResult, build_doctor_report, check_disk, check_gpu
from uefactory.core.config import DoctorConfig, HostConfig, Settings
from uefactory.core.remote import RemoteCommandResult
from uefactory.core.remote_probe import REMOTE_DOCTOR_SCRIPT, build_remote_doctor_report
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


def test_remote_doctor_report_uses_single_remote_probe(monkeypatch: Any, tmp_path: Path) -> None:
    report = {
        "schema_version": 1,
        "host": "remote-node",
        "remote_host": "l40s",
        "status": "OK",
        "duration_sec": 0.1,
        "paths": {"work_dir": "/remote/work", "engine_dir": "/remote/engine"},
        "checks": [
            {"name": "node_sentinel", "status": "OK", "message": "ok", "details": {}},
            {"name": "disk", "status": "WARN", "message": "warn", "details": {}},
        ],
    }
    calls: list[str] = []

    def fake_run(self: Any, command: str, *, timeout_sec: int = 60, check: bool = True) -> Any:
        calls.append(command)
        return RemoteCommandResult(
            command=["ssh", "l40s", command],
            returncode=0,
            stdout=json.dumps(report),
            stderr="",
            duration_sec=0.02,
        )

    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        hosts={
            "l40s": HostConfig(
                name="l40s",
                ssh_alias="l40s",
                work_dir=Path("/remote/work"),
                engine_dir=Path("/remote/engine"),
            )
        },
    )
    monkeypatch.setattr("uefactory.core.remote.RemoteHost.run", fake_run)

    remote_report = build_remote_doctor_report(settings, "l40s")

    assert len(calls) == 1
    assert remote_report["status"] == "WARN"
    assert remote_report["transport"]["ssh_connection_count"] == 1


def test_remote_doctor_script_warns_when_vulkaninfo_times_out(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    work_dir = tmp_path / "work"
    engine_dir = tmp_path / "engine"
    work_dir.mkdir()
    (work_dir / ".uef_node").write_text(
        json.dumps({"host": "4090"}),
        encoding="utf-8",
    )

    monkeypatch.setenv("UEF_HOST_NAME", "4090")
    monkeypatch.setenv("UEF_WORK_DIR", str(work_dir))
    monkeypatch.setenv("UEF_ENGINE_DIR", str(engine_dir))
    monkeypatch.setenv("UEF_GPU", "8x RTX 4090")
    monkeypatch.setenv("UEF_PROBE_ID", "probe-1")
    monkeypatch.setattr("platform.platform", lambda: "Linux-test")

    def fake_which(name: str) -> str | None:
        if name in {"nvidia-smi", "vulkaninfo"}:
            return f"/usr/bin/{name}"
        return None

    def fake_run(*args: Any, **kwargs: Any) -> CompletedProcess[str]:
        argv = args[0]
        if argv[0] == "ldconfig":
            return CompletedProcess(
                argv,
                0,
                "\tlibvulkan.so.1 (libc6,x86-64) => /lib/libvulkan.so.1\n",
                "",
            )
        if argv[0] == "/usr/bin/nvidia-smi":
            return CompletedProcess(argv, 0, "GPU, 24564, 20000, 550.67\n", "")
        if argv[0] == "/usr/bin/vulkaninfo":
            raise TimeoutExpired(argv, kwargs["timeout"])
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)

    exec(REMOTE_DOCTOR_SCRIPT, {})

    report = json.loads(capsys.readouterr().out)
    vulkan = next(check for check in report["checks"] if check["name"] == "vulkan")
    assert report["status"] == "WARN"
    assert vulkan["status"] == "WARN"
    assert "timed out" in vulkan["details"]["error"]
