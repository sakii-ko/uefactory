#!/usr/bin/env bash
set -euo pipefail
# 发送协作信号(门铃)。内容一律走文档,message 只放一两句话 + 文档路径。
# 用法: tools/signal.sh <收件人: planner|coder> <EVENT> [message...]

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
to="${1:?usage: signal.sh <planner|coder> <EVENT> [message...]}"
event="${2:?usage: signal.sh <planner|coder> <EVENT> [message...]}"
shift 2
message="${*:-}"

case "${to}" in
  planner|coder) ;;
  *) echo "error: <to> must be planner or coder" >&2; exit 1 ;;
esac
from="coder"
[[ "${to}" == "coder" ]] && from="planner"

dir="${repo_root}/signals/to_${to}"
mkdir -p "${dir}"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
branch="$(git -C "${repo_root}" branch --show-current 2>/dev/null || echo unknown)"
sha="$(git -C "${repo_root}" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# 先写临时文件再 mv,保证监听方不会读到半个文件
tmp="$(mktemp "${dir}/.tmp.XXXXXX")"
SIG_FROM="${from}" SIG_TO="${to}" SIG_EVENT="${event}" SIG_MSG="${message}" \
SIG_TS="${ts}" SIG_BRANCH="${branch}" SIG_SHA="${sha}" \
python3 - "${tmp}" <<'PY'
import json
import os
import sys

payload = {
    "schema_version": 1,
    "from": os.environ["SIG_FROM"],
    "to": os.environ["SIG_TO"],
    "event": os.environ["SIG_EVENT"],
    "message": os.environ["SIG_MSG"],
    "branch": os.environ["SIG_BRANCH"],
    "sha": os.environ["SIG_SHA"],
    "created_utc": os.environ["SIG_TS"],
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
PY
final="${dir}/${ts}_${event}_$$.json"
mv "${tmp}" "${final}"
echo "signal sent: ${final}"
