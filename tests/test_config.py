from __future__ import annotations

from pathlib import Path

from uefactory.core.config import load_settings


def test_config_file_overrides_defaults(tmp_path: Path) -> None:
    config = tmp_path / "uef.toml"
    config.write_text(
        """
[core]
ue_root = "engine"
data_dir = "custom-data"
log_dir = "custom-logs"
ddc_dir = "fast-ddc"

[doctor]
min_free_vram_gib = 12
nas_warn_write_mbps = 350
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(config_file=config, env={}, project_root=tmp_path)

    assert settings.ue_root == tmp_path / "engine"
    assert settings.data_dir == tmp_path / "custom-data"
    assert settings.log_dir == tmp_path / "custom-logs"
    assert settings.ddc_dir == tmp_path / "fast-ddc"
    assert settings.doctor.min_free_vram_gib == 12
    assert settings.doctor.nas_warn_write_mbps == 350


def test_environment_overrides_config_file(tmp_path: Path) -> None:
    config = tmp_path / "uef.toml"
    config.write_text(
        """
[core]
ue_root = "engine"
data_dir = "custom-data"
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(
        config_file=config,
        env={
            "UEF_UE_ROOT": "/opt/ue",
            "UEF_DATA_DIR": "env-data",
            "UEF_MIN_FREE_VRAM_GIB": "16",
        },
        project_root=tmp_path,
    )

    assert settings.ue_root == Path("/opt/ue")
    assert settings.data_dir == tmp_path / "env-data"
    assert settings.doctor.min_free_vram_gib == 16


def test_hosts_are_loaded_from_config(tmp_path: Path) -> None:
    config = tmp_path / "uef.toml"
    config.write_text(
        """
[hosts.l40s]
ssh_alias = "l40s"
work_dir = "/remote/work"
engine_dir = "/remote/engine"
gpu = "L40S"
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(config_file=config, env={}, project_root=tmp_path)

    assert settings.hosts["l40s"].ssh_alias == "l40s"
    assert settings.hosts["l40s"].work_dir == Path("/remote/work")
    assert settings.hosts["l40s"].engine_dir == Path("/remote/engine")
    assert settings.hosts["l40s"].gpu == "L40S"
