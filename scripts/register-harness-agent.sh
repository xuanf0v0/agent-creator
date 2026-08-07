#!/usr/bin/env bash
set -euo pipefail
HARNESS_ROOT="/Users/ypc/agent-harness"; BASE_URL="http://127.0.0.1:8765"
while [[ $# -gt 0 ]]; do case "$1" in --harness-root) HARNESS_ROOT="$2"; shift 2;; --base-url) BASE_URL="$2"; shift 2;; *) exit 2;; esac; done
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"; source "$HARNESS_ROOT/.runtime.env"
[[ -n "${AGENT_HARNESS_MANAGEMENT_TOKEN:-}" ]] || { echo "缺少 Harness 管理 Token" >&2; exit 1; }
PYTHON_BIN="$HARNESS_ROOT/.venv/bin/python"; ADAPTER_BIN="$HARNESS_ROOT/.venv/bin/openagent-harness-opencode"
MANIFEST=$(python3 - "$PROJECT_ROOT" "$PYTHON_BIN" "$ADAPTER_BIN" <<'PY'
import json,sys
project,python,adapter_bin=sys.argv[1:]
manifest={"id":"coding","name":"OpenCode Coding Agent","description":"Independent OpenCode adapter used by OpenAgent Studio","cwd":project,"env_file":f"{project}/.env","labels":{"runtime.example/implementation":"openagent-harness-opencode","runtime.example/model":"deepseek/deepseek-v4-flash"},"task":{"command":[adapter_bin,"--model","deepseek/deepseek-v4-flash","--agent","plan","--env-file",f"{project}/.env"],"protocol":{"kind":"stdin_json"},"verification":[{"name":"adapter import","command":[python,"-c","import openagent_harness_opencode"]}],"tools":{"allow":["read","network"],"deny":["write","edit","destructive"]},"sandbox":{"enabled":True,"backend":"auto","enforcement":"best_effort","network":"allow","workspace_write":False}}}
print(json.dumps(manifest,ensure_ascii=False))
PY
)
AUTH=(-H "Authorization: Bearer $AGENT_HARNESS_MANAGEMENT_TOKEN" -H "X-Harness-Supported-Versions: 1" -H "content-type: application/json")
if curl --fail --silent "$BASE_URL/api/v1/agents" "${AUTH[@]}" | python3 -c 'import json,sys; sys.exit(0 if any(x["id"]=="coding" for x in json.load(sys.stdin)) else 1)'; then curl --fail --silent -X PATCH "$BASE_URL/api/v1/agents/coding" "${AUTH[@]}" -d "{\"manifest\":$MANIFEST}" >/dev/null; else curl --fail --silent -X POST "$BASE_URL/api/v1/agents" "${AUTH[@]}" -d "{\"manifest\":$MANIFEST}" >/dev/null; fi
curl --fail --silent -X POST "$BASE_URL/api/agents/coding/setup" -H "Authorization: Bearer $AGENT_HARNESS_MANAGEMENT_TOKEN" -H "Idempotency-Key: openagent-coding-setup" >/dev/null
for _ in $(seq 1 120); do state=$(curl --fail --silent "$BASE_URL/api/agents/coding" -H "Authorization: Bearer $AGENT_HARNESS_MANAGEMENT_TOKEN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("lifecycle_state", ""))'); [[ "$state" == ready ]] && exit 0; [[ "$state" == error ]] && exit 1; sleep 0.5; done
echo "coding Agent setup 未 ready" >&2; exit 1
