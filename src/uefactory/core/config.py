from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from uefactory.core.paths import find_project_root, resolve_path

DEFAULT_UE_ROOT = Path("/root/nas/bigdata1/cjw/UnrealEngine_5.5.4")
DEFAULT_UE_HOME = Path("/root/nas/bigdata1/cjw/UE5Home")


@dataclass(frozen=True)
class DoctorConfig:
    min_free_vram_gib: float = 8.0
    nas_warn_write_mbps: float = 200.0
    write_test_mib: int = 512


@dataclass(frozen=True)
class HostConfig:
    name: str
    ssh_alias: str
    work_dir: Path
    engine_dir: Path
    gpu: str | None = None


@dataclass(frozen=True)
class Settings:
    project_root: Path
    ue_root: Path = DEFAULT_UE_ROOT
    ue_home: Path = DEFAULT_UE_HOME
    data_dir: Path = field(default_factory=lambda: Path("data"))
    log_dir: Path = field(default_factory=lambda: Path("logs"))
    ddc_dir: Path | None = None
    runtime_lib_dir: Path | None = None
    doctor: DoctorConfig = field(default_factory=DoctorConfig)
    hosts: dict[str, HostConfig] = field(default_factory=dict)


def load_settings(
    *,
    config_file: Path | None = None,
    env: Mapping[str, str] | None = None,
    project_root: Path | None = None,
) -> Settings:
    root = (project_root or find_project_root()).resolve()
    env_values = env if env is not None else os.environ
    config_path = config_file or root / "uef.toml"
    file_config = _read_toml(config_path)

    core_config = _dict_value(file_config, "core")
    doctor_config = _dict_value(file_config, "doctor")
    hosts_config = _dict_value(file_config, "hosts")

    ue_root = _path_setting(
        "UEF_UE_ROOT", "ue_root", core_config, env_values, root, DEFAULT_UE_ROOT
    )
    ue_home = _path_setting(
        "UEF_UE_HOME", "ue_home", core_config, env_values, root, DEFAULT_UE_HOME
    )
    data_dir = _path_setting(
        "UEF_DATA_DIR", "data_dir", core_config, env_values, root, root / "data"
    )
    log_dir = _path_setting("UEF_LOG_DIR", "log_dir", core_config, env_values, root, root / "logs")
    ddc_dir = _optional_path_setting("UEF_DDC_DIR", "ddc_dir", core_config, env_values, root)
    runtime_lib_dir = _optional_path_setting(
        "UEF_RUNTIME_LIB_DIR", "runtime_lib_dir", core_config, env_values, root
    )

    doctor = DoctorConfig(
        min_free_vram_gib=_float_setting(
            "UEF_MIN_FREE_VRAM_GIB",
            "min_free_vram_gib",
            doctor_config,
            env_values,
            DoctorConfig.min_free_vram_gib,
        ),
        nas_warn_write_mbps=_float_setting(
            "UEF_NAS_WARN_WRITE_MBPS",
            "nas_warn_write_mbps",
            doctor_config,
            env_values,
            DoctorConfig.nas_warn_write_mbps,
        ),
        write_test_mib=_int_setting(
            "UEF_DOCTOR_WRITE_TEST_MIB",
            "write_test_mib",
            doctor_config,
            env_values,
            DoctorConfig.write_test_mib,
        ),
    )

    hosts = _load_hosts(hosts_config, env_values, root)

    return Settings(
        project_root=root,
        ue_root=ue_root,
        ue_home=ue_home,
        data_dir=data_dir,
        log_dir=log_dir,
        ddc_dir=ddc_dir,
        runtime_lib_dir=runtime_lib_dir,
        doctor=doctor,
        hosts=hosts,
    )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as file:
        value = tomllib.load(file)
    if not isinstance(value, dict):
        msg = f"Config file {path} did not parse to a TOML table"
        raise ValueError(msg)
    return value


def _dict_value(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key, {})
    if not isinstance(value, dict):
        msg = f"Config key [{key}] must be a table"
        raise ValueError(msg)
    return value


def _path_setting(
    env_key: str,
    config_key: str,
    config: dict[str, Any],
    env: Mapping[str, str],
    root: Path,
    default: Path,
) -> Path:
    if env_key in env:
        return resolve_path(env[env_key], root)
    if config_key in config:
        return resolve_path(_stringish(config[config_key], config_key), root)
    return resolve_path(default, root)


def _optional_path_setting(
    env_key: str,
    config_key: str,
    config: dict[str, Any],
    env: Mapping[str, str],
    root: Path,
) -> Path | None:
    if env_key in env:
        return resolve_path(env[env_key], root)
    if config_key in config:
        return resolve_path(_stringish(config[config_key], config_key), root)
    return None


def _float_setting(
    env_key: str,
    config_key: str,
    config: dict[str, Any],
    env: Mapping[str, str],
    default: float,
) -> float:
    value: str | int | float
    value = env[env_key] if env_key in env else config.get(config_key, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        msg = f"{config_key} must be a number"
        raise ValueError(msg) from exc


def _int_setting(
    env_key: str,
    config_key: str,
    config: dict[str, Any],
    env: Mapping[str, str],
    default: int,
) -> int:
    value: str | int
    value = env[env_key] if env_key in env else config.get(config_key, default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        msg = f"{config_key} must be an integer"
        raise ValueError(msg) from exc


def _load_hosts(
    hosts_config: dict[str, Any],
    env: Mapping[str, str],
    root: Path,
) -> dict[str, HostConfig]:
    hosts: dict[str, HostConfig] = {}
    for name, raw_host in hosts_config.items():
        if not isinstance(raw_host, dict):
            msg = f"Config key [hosts.{name}] must be a table"
            raise ValueError(msg)
        ssh_alias = _stringish(
            env.get(f"UEF_HOST_{name.upper()}_SSH_ALIAS", raw_host.get("ssh_alias", name)),
            f"hosts.{name}.ssh_alias",
        )
        work_dir_raw = env.get(f"UEF_HOST_{name.upper()}_WORK_DIR", raw_host.get("work_dir"))
        engine_dir_raw = env.get(f"UEF_HOST_{name.upper()}_ENGINE_DIR", raw_host.get("engine_dir"))
        if work_dir_raw is None or engine_dir_raw is None:
            msg = f"hosts.{name} requires work_dir and engine_dir"
            raise ValueError(msg)
        gpu_raw = env.get(f"UEF_HOST_{name.upper()}_GPU", raw_host.get("gpu"))
        hosts[name] = HostConfig(
            name=name,
            ssh_alias=ssh_alias,
            work_dir=resolve_path(_stringish(work_dir_raw, f"hosts.{name}.work_dir"), root),
            engine_dir=resolve_path(_stringish(engine_dir_raw, f"hosts.{name}.engine_dir"), root),
            gpu=None if gpu_raw is None else _stringish(gpu_raw, f"hosts.{name}.gpu"),
        )
    return hosts


def _stringish(value: Any, key: str) -> str:
    if isinstance(value, str | int | float):
        return str(value)
    msg = f"{key} must be a string-like value"
    raise ValueError(msg)
