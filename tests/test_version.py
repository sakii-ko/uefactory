from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

from uefactory import __version__


def test_package_versions_stay_in_sync() -> None:
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert project["project"]["version"] == "0.3.0"
    assert __version__ == project["project"]["version"]
    assert version("uefactory") == project["project"]["version"]
