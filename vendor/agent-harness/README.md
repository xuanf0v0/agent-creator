# Agent Harness

A declarative, local-only runtime harness for HTTP agents. It extracts process
supervision, health checks, logs, editable environment configuration, and HTTP
proxying from business-specific agent platforms.

It also provides a governance layer for one-shot coding agents: repository
instructions, reproducible-environment checks, durable task state, and mandatory
verification before a task can be marked complete.

## Quick start

```bash
uv sync --extra dev
uv run agent-harness validate agents
uv run agent-harness serve --manifests agents
```

In another terminal:

```bash
uv run agent-harness setup echo
uv run agent-harness start echo
curl -s http://127.0.0.1:8765/api/agents/echo/service/echo \
  -H 'content-type: application/json' -d '{"message":"hello"}'
uv run agent-harness logs echo
uv run agent-harness stop echo
```

Governed task example (after running `setup coding`):

```bash
uv run agent-harness setup coding
uv run agent-harness instructions coding
uv run agent-harness task create coding "Example task" "Write the requested output"
uv run agent-harness task list --agent-id coding
```

The coding command receives one JSON document on stdin containing the task,
merged instructions, declarative tool policy, and completion contract. Exit 0
requests verification; the task becomes `completed` only when every declared
verification command succeeds.

The management API binds to `127.0.0.1:8765`. Set `AGENT_HARNESS_TOKEN` to
require a Bearer token for management HTTP and WebSocket APIs. A non-local bind
is refused unless that token is configured. CLI requests automatically use the
same environment variable and callers can identify themselves with `X-Actor`.

## Agent manifest

Place one YAML document per agent under `agents/`. Commands are argv arrays and
are never run through a shell. Relative `cwd` values resolve from the manifest;
`env_file` resolves from the agent working directory.

```yaml
id: my-agent
name: My Agent
cwd: ../path/to/agent
environment:
  setup_command: [uv, sync, --frozen]
  auto_setup_on_drift: false
service:
  command: [uv, run, uvicorn, app:app, --host, 127.0.0.1, --port, "9001"]
  port: 9001
  health: {path: /health, timeout_seconds: 15}
task:
  command: [my-coding-agent, --stdin]
  sandbox:
    enabled: true
    enforcement: required
    network: allowlist
    network_allowlist: [api.example.com]
    workspace_write: true
  limits:
    command_timeout_seconds: 3600
    attempt_timeout_seconds: 5400
    max_log_bytes: 52428800
    max_log_lines: 50000
    max_queue_depth: 100
  verification:
    - {name: tests, command: [uv, run, pytest]}
env_file: .env
config:
  - {key: API_KEY, label: API key, type: secret, default: ""}
```

Every manifest has a validated setup command; omitted setup uses a safe no-op.
Setup is explicit by default, while `auto_setup_on_drift: true` lets the single
Agent worker prepare a drifted environment before executing its task. `start`
refuses occupied ports. The harness only
stops process groups that it started itself and stops all owned agents during a
normal management-service shutdown.

Before running a task, the harness fingerprints dependency/environment files.
The setup command itself is part of the persisted environment fingerprint. If
it or a dependency file changes since setup, the task reports `setup_required` with structured
`error_code`, current/previous fingerprints and changed files. Task and attempt state
is stored under `$AGENT_HARNESS_HOME/state.db` (default `~/.agent-harness`) and
a local `.harness/PROGRESS.md` is regenerated in the managed workspace.

Instructions are merged in root-to-leaf order from root `AGENTS.md`, root
`CLAUDE.md`, and nested `AGENTS.md` files down to the selected task directory.
Task commands and verification checks run inside an OS sandbox by default.
macOS uses Seatbelt and Linux uses Bubblewrap. Writes are restricted to the
Agent workspace and temporary directory. Network defaults to `deny`; manifests
may choose `allow` or a hostname allowlist. `required` fails closed
when the platform backend is unavailable; `best_effort` is an explicit opt-out.
An allowlist is enforced through a per-command localhost proxy: Seatbelt permits
only that exact proxy port, and the proxy rejects destinations not named in the
manifest. Denying `write` or `edit` in the tool policy makes the workspace
read-only; denying `network` requires the runtime network mode to remain `deny`.

## Reliability

One harness home is owned by one running process through an OS-backed lock.
SQLite uses WAL, foreign keys, schema migrations, migration backups, integrity
checks and conditional state transitions. A second process using the same
`AGENT_HARNESS_HOME` is rejected before it can migrate or consume tasks.

Task creation and retry accept `Idempotency-Key` (`--idempotency-key` in the
CLI). Replaying the same request returns the original resource; reusing the key
with a different payload returns `409`. Agent and verification failures are not
automatically rerun.

Setup has its own idempotency scope and persistent operation records, separate
from task attempts and retries. Use `Prefer: respond-async` to receive `202`,
then poll `/api/setup-operations/{id}`. Status, fingerprint, bounded logs,
structured failure, start time and finish time survive a Harness restart.
Interrupted setup operations recover as `error/harness_interrupted`; queued
tasks are restored and interrupted task attempts are recorded as failed.

The default command/attempt limits are 60/90 minutes, 50 MiB or 50,000 persisted
log lines, and 100 queued tasks per agent. Log overflow is drained but not
stored and emits `log.truncated`; it does not fail otherwise valid work.

On shutdown, the harness stops accepting work, waits up to 10 seconds, then
terminates owned process groups and records interrupted tasks. Completed-task
logs older than 30 days are removed on startup and every 24 hours; structured
task, attempt, event and verification evidence is retained.

Operational checks:

```bash
curl -s http://127.0.0.1:8765/ready
uv run agent-harness doctor
uv run agent-harness maintain
```

Database integrity failure or less than 256 MiB free disk makes the service
not-ready: reads, stop and cancel remain available while new mutations are
rejected. Between 256 MiB and 1 GiB free disk is reported as a warning.

## Management API

- `GET /health`, `GET /api/agents`, `GET /api/agents/{id}`
- `POST /api/agents/{id}/{setup|start|stop|restart}`
- `GET /api/agents/{id}/setup`, `GET /api/setup-operations/{id}`
- `GET /api/agents/{id}/logs`, `WS /ws/agents/{id}/logs`
- `GET|PUT /api/agents/{id}/config`
- `/api/agents/{id}/service/{path}` forwards HTTP and streaming responses
- `GET /api/agents/{id}/instructions`, `GET /api/agents/{id}/environment`
- `POST /api/tasks`, task list/detail/events/logs/retry/cancel endpoints
- `GET /ready`, `GET /api/system/status`, `POST /api/system/maintain`
- `POST /api/system/reload` (manifests are also watched and reloaded automatically)
- `GET /api/system/audit`, `GET /metrics`, `GET /api/events/stream` (SSE)
