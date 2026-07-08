from __future__ import annotations

import configparser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CaseSensitiveConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _read_default_engine_ini() -> configparser.ConfigParser:
    parser = CaseSensitiveConfigParser(strict=False)
    parser.read_string(
        (PROJECT_ROOT / "ue/UEFBase/Config/DefaultEngine.ini").read_text(encoding="utf-8")
    )
    return parser


def test_headless_project_disables_online_request_sources() -> None:
    config = _read_default_engine_ini()

    assert config["HTTP"]["bEnableHttp"] == "False"
    assert config["HTTP"]["bUseNullHttp"] == "True"
    assert config["OnlineSubsystem"]["DefaultPlatformService"] == ""
    assert config["OnlineSubsystem"]["NativePlatformService"] == ""
    assert config["/Script/Engine.AnalyticsSettings"]["bUseAnalytics"] == "False"
    assert config["StudioTelemetry.Config"]["SendTelemetry"] == "false"
    assert config["StudioTelemetry.Config"]["SendHardwareData"] == "false"
    assert config["StudioTelemetry.Config"]["SendOSData"] == "false"
