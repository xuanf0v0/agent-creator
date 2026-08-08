#!/usr/bin/env bash
set -euo pipefail
HARNESS_ROOT="/Users/ypc/agent-harness"; BASE_URL="http://127.0.0.1:8765"
while [[ $# -gt 0 ]]; do case "$1" in --harness-root) HARNESS_ROOT="$2"; shift 2;; --base-url) BASE_URL="$2"; shift 2;; *) exit 2;; esac; done
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"; source "$HARNESS_ROOT/.runtime.env"
[[ -n "${AGENT_HARNESS_MANAGEMENT_TOKEN:-}" ]] || { echo "缺少 Harness 管理 Token" >&2; exit 1; }
[[ -n "${AGENT_HARNESS_TASK_TOKEN:-}" ]] || { echo "缺少 Harness 任务 Token" >&2; exit 1; }
PYTHON_BIN="$HARNESS_ROOT/.venv/bin/python"; ADAPTER_BIN="$HARNESS_ROOT/.venv/bin/openagent-harness-opencode"
MANIFEST=$(python3 - "$PROJECT_ROOT" "$PYTHON_BIN" "$ADAPTER_BIN" <<'PY'
import json,sys
project,python,adapter_bin=sys.argv[1:]
manifest={"id":"coding","name":"OpenCode Text Coding Agent","description":"Independent no-tools OpenCode text adapter used by OpenAgent Studio","cwd":project,"env_file":f"{project}/.env","labels":{"runtime.example/implementation":"openagent-harness-opencode","runtime.example/model":"deepseek/deepseek-v4-flash","runtime.example/capability":"text-generation","runtime.example/sandbox":"read-only"},"task":{"command":[adapter_bin,"--model","deepseek/deepseek-v4-flash","--agent","openagent-runtime-text","--env-file",f"{project}/.env"],"protocol":{"kind":"stdin_json"},"verification":[{"name":"adapter import","command":[python,"-c","import openagent_harness_opencode"]}],"tools":{"allow":["network"],"deny":["read","write","edit","bash","task","destructive"]},"sandbox":{"enabled":True,"backend":"auto","enforcement":"best_effort","network":"allow","workspace_write":False}}}
print(json.dumps(manifest,ensure_ascii=False))
PY
)
AUTH=(-H "Authorization: Bearer $AGENT_HARNESS_MANAGEMENT_TOKEN" -H "X-Harness-Supported-Versions: 1" -H "content-type: application/json")
if curl --fail --silent "$BASE_URL/api/v1/agents" "${AUTH[@]}" | python3 -c 'import json,sys; sys.exit(0 if any(x["id"]=="coding" for x in json.load(sys.stdin)) else 1)'; then curl --fail --silent -X PATCH "$BASE_URL/api/v1/agents/coding" "${AUTH[@]}" -d "{\"manifest\":$MANIFEST}" >/dev/null; else curl --fail --silent -X POST "$BASE_URL/api/v1/agents" "${AUTH[@]}" -d "{\"manifest\":$MANIFEST}" >/dev/null; fi
SETUP_KEY="openagent-coding-setup-$(printf '%s' "$MANIFEST" | shasum -a 256 | awk '{print $1}')"
curl --fail --silent -X POST "$BASE_URL/api/agents/coding/setup" -H "Authorization: Bearer $AGENT_HARNESS_MANAGEMENT_TOKEN" -H "Idempotency-Key: $SETUP_KEY" >/dev/null
for _ in $(seq 1 120); do state=$(curl --fail --silent "$BASE_URL/api/agents/coding" -H "Authorization: Bearer $AGENT_HARNESS_MANAGEMENT_TOKEN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("lifecycle_state", ""))'); [[ "$state" == ready ]] && break; [[ "$state" == error ]] && exit 1; sleep 0.5; done
[[ "${state:-}" == ready ]] || { echo "coding Agent setup 未 ready" >&2; exit 1; }
descriptor=$(curl --fail --silent "$BASE_URL/api/v1/task-agents/coding" -H "Authorization: Bearer $AGENT_HARNESS_TASK_TOKEN" -H "X-Harness-Supported-Versions: 1")
printf '%s' "$descriptor" | python3 -c 'import json,sys; d=json.load(sys.stdin); r=d.get("readiness",{}); labels=d.get("labels",{}); ok=d.get("enabled") and d.get("accepts_tasks") and r.get("state")=="ready" and d.get("protocol",{}).get("kind")=="stdin_json" and labels.get("runtime.example/implementation")=="openagent-harness-opencode" and labels.get("runtime.example/capability")=="text-generation"; raise SystemExit(0 if ok else "coding task-agent descriptor mismatch")'
