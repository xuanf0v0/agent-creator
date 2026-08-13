#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS_ROOT="${PROJECT_ROOT}/my-harness"; BASE_URL="http://127.0.0.1:8765"
while [[ $# -gt 0 ]]; do case "$1" in --harness-root) HARNESS_ROOT="$2"; shift 2;; --base-url) BASE_URL="$2"; shift 2;; *) exit 2;; esac; done
source "$HARNESS_ROOT/.runtime.env"
[[ -n "${AGENT_HARNESS_MANAGEMENT_TOKEN:-}" ]] || { echo "缺少 Harness 管理 Token" >&2; exit 1; }
[[ -n "${AGENT_HARNESS_TASK_TOKEN:-}" ]] || { echo "缺少 Harness 任务 Token" >&2; exit 1; }
PYTHON_BIN="$HARNESS_ROOT/.venv/bin/python"; ADAPTER_BIN="$HARNESS_ROOT/.venv/bin/openagent-harness-opencode"
AUTH=(-H "Authorization: Bearer $AGENT_HARNESS_MANAGEMENT_TOKEN" -H "X-Harness-Supported-Versions: 1" -H "content-type: application/json")
DEFINITIONS=(
  'coding|OpenCode Text Coding Agent|Independent no-tools OpenCode text adapter used by OpenAgent Studio|openagent-runtime-text|text-generation|network|read,write,edit,bash,task,destructive'
  'repository-analysis|OpenCode Repository Analysis Agent|Read-only repository analysis through OpenCode|openagent-runtime-analysis|repository-analysis|network,read|write,edit,bash,task,destructive'
  'test-runner|OpenCode Read-only Test Agent|Runs declared tests without writing project files|openagent-runtime-tests|test-execution|network,bash|read,write,edit,task,destructive'
)
for definition in "${DEFINITIONS[@]}"; do
  IFS='|' read -r agent_id agent_name agent_description opencode_agent capability allow_csv deny_csv <<<"$definition"
  MANIFEST=$(python3 - "$PROJECT_ROOT" "$PYTHON_BIN" "$ADAPTER_BIN" "$agent_id" "$agent_name" "$agent_description" "$opencode_agent" "$capability" "$allow_csv" "$deny_csv" <<'PY'
import json,sys
project,python,adapter_bin,agent_id,name,description,opencode_agent,capability,allow_csv,deny_csv=sys.argv[1:]
split=lambda value:[item for item in value.split(",") if item]
manifest={"id":agent_id,"name":name,"description":description,"cwd":project,"env_file":f"{project}/.env","labels":{"runtime.example/implementation":"openagent-harness-opencode","runtime.example/model":"deepseek/deepseek-v4-flash","runtime.example/capability":capability,"runtime.example/sandbox":"read-only"},"task":{"command":[adapter_bin,"--model","deepseek/deepseek-v4-flash","--agent",opencode_agent,"--env-file",f"{project}/.env"],"protocol":{"kind":"stdin_json"},"verification":[{"name":"adapter import","command":[python,"-c","import openagent_harness_opencode"]}],"tools":{"allow":split(allow_csv),"deny":split(deny_csv)},"sandbox":{"enabled":True,"backend":"auto","enforcement":"best_effort","network":"allow","workspace_write":False}}}
print(json.dumps(manifest,ensure_ascii=False))
PY
  )
  if curl --fail --silent "$BASE_URL/api/v1/agents" "${AUTH[@]}" | python3 -c "import json,sys; sys.exit(0 if any(x['id']=='$agent_id' for x in json.load(sys.stdin)) else 1)"; then curl --fail --silent -X PATCH "$BASE_URL/api/v1/agents/$agent_id" "${AUTH[@]}" -d "{\"manifest\":$MANIFEST}" >/dev/null; else curl --fail --silent -X POST "$BASE_URL/api/v1/agents" "${AUTH[@]}" -d "{\"manifest\":$MANIFEST}" >/dev/null; fi
  SETUP_KEY="openagent-$agent_id-setup-$(printf '%s' "$MANIFEST" | shasum -a 256 | awk '{print $1}')"
  curl --fail --silent -X POST "$BASE_URL/api/agents/$agent_id/setup" -H "Authorization: Bearer $AGENT_HARNESS_MANAGEMENT_TOKEN" -H "Idempotency-Key: $SETUP_KEY" >/dev/null
  state=""
  for _ in $(seq 1 120); do state=$(curl --fail --silent "$BASE_URL/api/agents/$agent_id" -H "Authorization: Bearer $AGENT_HARNESS_MANAGEMENT_TOKEN" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("lifecycle_state", ""))'); [[ "$state" == ready ]] && break; [[ "$state" == error ]] && exit 1; sleep 0.5; done
  [[ "$state" == ready ]] || { echo "$agent_id Agent setup 未 ready" >&2; exit 1; }
  descriptor=$(curl --fail --silent "$BASE_URL/api/v1/task-agents/$agent_id" -H "Authorization: Bearer $AGENT_HARNESS_TASK_TOKEN" -H "X-Harness-Supported-Versions: 1")
  printf '%s' "$descriptor" | python3 -c "import json,sys; d=json.load(sys.stdin); r=d.get('readiness',{}); labels=d.get('labels',{}); ok=d.get('enabled') and d.get('accepts_tasks') and r.get('state')=='ready' and d.get('protocol',{}).get('kind')=='stdin_json' and labels.get('runtime.example/implementation')=='openagent-harness-opencode' and labels.get('runtime.example/capability')=='$capability'; raise SystemExit(0 if ok else '$agent_id task-agent descriptor mismatch')"
  echo "$agent_id Agent 已注册到独立 Harness，环境状态已准备"
done
