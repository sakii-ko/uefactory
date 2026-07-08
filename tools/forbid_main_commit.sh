#!/usr/bin/env bash
set -euo pipefail

branch="$(git branch --show-current)"
if [[ "${branch}" == "main" ]]; then
  if [[ "${UEF_ALLOW_MAIN:-}" == "1" ]]; then
    echo "UEF_ALLOW_MAIN=1 set; allowing direct commit to main." >&2
    exit 0
  fi
  echo "Direct commits to main are not allowed. Create a feature branch first." >&2
  exit 1
fi
