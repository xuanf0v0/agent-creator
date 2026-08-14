# Spec: LLM Agent Loop Workflow Generation

## Objective

Replace server-owned generation stages with one model-driven agent loop. LLM chooses graph edits, validation/evaluation actions, diagnosis, repair, continuation, and user questions. Server remains a deterministic tool host: validates structured actions, enforces a finite DAG, executes Harness evaluation, persists only a verified workflow, and honors cancellation.

## Commands

- Focused tests: `& .\.venv\Scripts\python.exe -m pytest tests/test_creator_toolcalls.py tests/test_creator_direct.py tests/test_studio.py -q`
- Full tests: `& .\.venv\Scripts\python.exe -m pytest tests/ -q`
- Frontend build: `npm.cmd --prefix frontend run build`
- Runtime startup: `& .\scripts\start-all.ps1`

## Project Structure

- `openagent_studio/generator.py` — model-driven loop, action protocol, persistence boundary
- `openagent_studio/workflow_runner.py` — executable DAG validation and Harness runtime
- `openagent_studio/evaluation.py` — real Harness acceptance and semantic verdict
- `openagent_studio/creator/` — public Creator API and SSE adapter
- `frontend/src/` — generation progress and question/answer UI
- `tests/` — Python unit/integration coverage

## Architecture Decisions

1. One `agent_loop` mode becomes default for create, modify, repair, and optimize. Legacy blueprint/incremental modes remain opt-in only for rollback.
2. Agent action protocol is additive and backward-compatible: `add_node`, `update_node`, `delete_node`, `evaluate`, `finalize`, and `ask_user`; legacy `complete` remains an alias for `evaluate`.
3. LLM controls next action. Server never advances a fixed stage sequence; it only applies one action batch, returns evidence, and invokes the model again.
4. DAG is enforced at every accepted graph mutation and at final runtime validation. Graph cycles must be represented by existing bounded `loop`/`iteration` nodes, never back-edges.
5. No generation-iteration cap by default. Cancellation, provider/process hard failure, Harness infrastructure failure, ETag conflict, and explicit user question are terminal/waiting boundaries. Timeout recovery remains same-model automatic retry.
6. Evaluation is an LLM-selected tool. A passing evaluation is required before `finalize` can persist; failed evidence is returned to the next model turn.

## Testing Strategy

- Unit tests for action normalization, DAG rejection, evaluate/finalize protocol, unlimited loop behavior, and user-question event state.
- Integration tests for generation routing and save-on-verified-only behavior.
- Existing Harness runtime tests remain the source of truth for execution semantics.
- Frontend build validates additive SSE/UI contract.

## Boundaries

- Always: preserve `WorkflowSpec`, public APIs, SSE event compatibility, ETag saves, cancellation, and project-local Harness boundary.
- Ask first: changing public REST payloads, changing the `WorkflowSpec` schema, or editing `./my-harness`/`adapters/opencode/`.
- Never: bypass DAG validation, save an unverified graph, use fake Harness success, expose secrets, or convert model timeouts into a user repair dialog.

## Success Criteria

- Default create/modify/optimize path enters `agent_loop`; no fixed candidate-attempt or stage loop owns next action.
- LLM can make multiple graph edits, request evaluation, repair from actual failure evidence, and continue until verification succeeds.
- Cyclic edge proposals are rejected with structured feedback; valid DAGs execute normally.
- `ask_user` emits a recoverable question without `generation.stalled` timeout UI; next user message resumes same draft/context.
- Only verified DAG is saved; existing public API/SSE and legacy escape-hatch tests remain green.

## Open Questions

- None blocking implementation; user requirement defines unbounded model loop with cancellation as safety boundary.
