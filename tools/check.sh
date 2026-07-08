#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON:-python}"
if [[ -x ".venv/bin/python" ]]; then
  python_bin=".venv/bin/python"
fi

"${python_bin}" -m ruff check .
"${python_bin}" -m ruff format --check .
"${python_bin}" -m mypy src tests
"${python_bin}" -m pytest
