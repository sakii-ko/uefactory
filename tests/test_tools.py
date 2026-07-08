from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_forbid_main_commit_allows_explicit_escape(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'if [[ "${1:-}" == "branch" && "${2:-}" == "--show-current" ]]; then',
                "  echo main",
                "  exit 0",
                "fi",
                "exit 2",
            ]
        ),
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["UEF_ALLOW_MAIN"] = "1"

    result = subprocess.run(
        [str(project_root / "tools/forbid_main_commit.sh")],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "UEF_ALLOW_MAIN=1" in result.stderr
