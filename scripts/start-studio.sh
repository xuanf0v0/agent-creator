#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS_ROOT="${HARNESS_ROOT:-/Users/ypc/agent-harness}"
HARNESS_URL="${HARNESS_URL:-http://127.0.0.1:8765}"
RUNTIME_ENV="$HARNESS_ROOT/.runtime.env"
[[ -x "$HARNESS_ROOT/.venv/bin/agent-harness" ]] || { echo "Harness 未安装：$HARNESS_ROOT" >&2; exit 1; }
[[ -f "$RUNTIME_ENV" ]] || { echo "缺少 Harness 运行时密钥文件：$RUNTIME_ENV" >&2; exit 1; }
set -a; source "$RUNTIME_ENV"; set +a
[[ -n "${AGENT_HARNESS_MANAGEMENT_TOKEN:-}" ]] || { echo "缺少 Harness 管理 Token（仅运维脚本需要）" >&2; exit 1; }
[[ -n "${AGENT_HARNESS_TASK_TOKEN:-}" ]] || { echo "缺少 Harness 任务 Token" >&2; exit 1; }
EXPECTED_INSTANCE_FILE="$HARNESS_ROOT/state/instance.json"
verify_instance() {
  [[ -f "$EXPECTED_INSTANCE_FILE" ]] || { echo "8765 已有 Harness，但缺少预期 state instance；拒绝接入未知实例" >&2; return 1; }
  local expected actual
  expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["instance_id"])' "$EXPECTED_INSTANCE_FILE")
  actual=$(curl --fail --silent --max-time 2 "$HARNESS_URL/api/v1/capabilities" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("instance",{}).get("instance_id", ""))')
  [[ -n "$expected" && "$expected" == "$actual" ]] || { echo "8765 上的 Harness instance 与预期 state 不匹配；拒绝继续" >&2; return 1; }
}
if ! curl --fail --silent --max-time 2 "$HARNESS_URL/api/v1/capabilities" >/dev/null 2>&1; then
  mkdir -p "$PROJECT_ROOT/.harness"
  AGENT_HARNESS_HOME="$HARNESS_ROOT/state" AGENT_HARNESS_ALLOWED_ROOTS="$PROJECT_ROOT" "$HARNESS_ROOT/.venv/bin/agent-harness" serve --manifests "$HARNESS_ROOT/manifests" --host 127.0.0.1 --port 8765 >"$PROJECT_ROOT/.harness/start-harness.stdout.log" 2>"$PROJECT_ROOT/.harness/start-harness.stderr.log" &
  for _ in $(seq 1 60); do curl --fail --silent --max-time 2 "$HARNESS_URL/api/v1/capabilities" >/dev/null 2>&1 && break; sleep 0.5; done
fi
verify_instance
"$PROJECT_ROOT/scripts/register-harness-agent.sh" --harness-root "$HARNESS_ROOT" --base-url "$HARNESS_URL"
export AGENT_HARNESS_URL="$HARNESS_URL" OPENAGENT_SPEC="${OPENAGENT_SPEC:-$PROJECT_ROOT/project.yaml}"
exec "$PROJECT_ROOT/.venv/bin/python" -m openagent_studio.app
