#!/usr/bin/env bash
set -euo pipefail

branch="$(git branch --show-current)"
if [[ "${branch}" == "main" ]]; then
  echo "Direct commits to main are not allowed. Create a feature branch first." >&2
  exit 1
fi
