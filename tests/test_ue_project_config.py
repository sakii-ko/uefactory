from __future__ import annotations

import configparser
import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CaseSensitiveConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _read_ini(name: str) -> configparser.ConfigParser:
    parser = CaseSensitiveConfigParser(strict=False)
    parser.read_string((PROJECT_ROOT / f"ue/UEFBase/Config/{name}").read_text(encoding="utf-8"))
    return parser


def _read_uproject() -> dict[str, Any]:
    return json.loads((PROJECT_ROOT / "ue/UEFBase/UEFBase.uproject").read_text(encoding="utf-8"))


def test_headless_project_disables_online_request_sources() -> None:
    config = _read_ini("DefaultEngine.ini")

    assert config["HTTP"]["bEnableHttp"] == "False"
    assert config["HTTP"]["bUseNullHttp"] == "True"
    assert config["OnlineSubsystem"]["DefaultPlatformService"] == ""
    assert config["OnlineSubsystem"]["NativePlatformService"] == ""


def test_headless_project_disables_editor_analytics() -> None:
    engine_config = _read_ini("DefaultEngine.ini")
    editor_config = _read_ini("DefaultEditorSettings.ini")

    assert "/Script/Engine.AnalyticsSettings" not in engine_config
    assert "StudioTelemetry.Config" not in engine_config
    assert editor_config["/Script/UnrealEd.AnalyticsPrivacySettings"]["bSendUsageData"] == "False"


def test_headless_project_disables_telemetry_plugins_without_disabling_mrq() -> None:
    project = _read_uproject()
    plugins = {entry["Name"]: entry["Enabled"] for entry in project["Plugins"]}

    assert plugins["EditorTelemetry"] is False
    assert plugins["EditorPerformance"] is False
    assert plugins["StudioTelemetry"] is False
    assert plugins["MovieRenderPipeline"] is True
    assert plugins["HDRIBackdrop"] is True
