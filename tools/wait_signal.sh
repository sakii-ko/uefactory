#!/usr/bin/env bash
set -euo pipefail
# 阻塞等待发给自己的协作信号;收到后打印(单行 JSON/个)、归档并以 0 退出。
# 超时以 2 退出(重启监听即可)。适合放在后台任务里跑。
# 用法: tools/wait_signal.sh <我是: planner|coder> [timeout_sec=7200] [poll_sec=15]

me="${1:?usage: wait_signal.sh <planner|coder> [timeout_sec] [poll_sec]}"
timeout_sec="${2:-7200}"
poll_sec="${3:-15}"
case "${me}" in
  planner|coder) ;;
  *) echo "error: <me> must be planner or coder" >&2; exit 1 ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dir="${repo_root}/signals/to_${me}"
archive="${repo_root}/signals/archive"
mkdir -p "${dir}" "${archive}"

elapsed=0
shopt -s nullglob
while :; do
  files=("${dir}"/*.json)
  if (( ${#files[@]} > 0 )); then
    for f in "${files[@]}"; do
      cat "${f}"
      mv "${f}" "${archive}/"
    done
    exit 0
  fi
  if (( elapsed >= timeout_sec )); then
    echo "TIMEOUT: no signal for ${me} after ${timeout_sec}s" >&2
    exit 2
  fi
  sleep "${poll_sec}"
  elapsed=$(( elapsed + poll_sec ))
done
