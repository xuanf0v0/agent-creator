# Task List: LLM Agent Loop Workflow Generation

## Phase 1: Contract and graph safety

- [x] Add agent-loop action contract and deterministic DAG mutation validation.
  - Acceptance: invalid action, unknown node, and cycle rejected with structured evidence; existing toolcalls actions still parse.
  - Verify: focused unit tests.
  - Files: `openagent_studio/generator.py`, `openagent_studio/workflow_runner.py`, `tests/test_creator_toolcalls.py`.

- [x] Route all generation operations to agent loop by default, retaining explicit legacy modes.
  - Acceptance: create/modify/optimize select `agent_loop`; no default blueprint/direct/max-attempt loop.
  - Verify: routing tests and focused generator tests.
  - Files: `openagent_studio/generator.py`, `tests/test_creator_toolcalls.py`, `tests/test_creator_direct.py`.

## Checkpoint: Contract

- [x] Focused backend tests pass.

## Phase 2: Model-controlled loop

- [x] Let LLM choose `evaluate`, `finalize`, and `ask_user`; feed tool evidence back into next turn.
  - Acceptance: evaluation is explicit, failed evaluation does not save, successful finalize saves.
  - Verify: loop tests with fake model/evaluator.
  - Files: `openagent_studio/generator.py`, `tests/test_creator_toolcalls.py`, `tests/test_studio.py`.

- [x] Add question/resume event path without timeout stalled dialog.
  - Acceptance: question is observable over SSE and user message resumes same draft.
  - Verify: backend route tests and frontend build.
  - Files: `openagent_studio/generator.py`, `openagent_studio/creator/generator.py`, `frontend/src/App.tsx`, tests.

## Checkpoint: Agent loop

- [x] Focused tests and frontend build pass.
- [x] Local runtime smoke test reaches a verified DAG or exposes exact external failure.

## Phase 3: Regression and handoff

- [x] Run full backend suite and frontend build.
- [ ] Cross-review diff with Claude; report uncommitted changes for Claude to commit/push.

Evidence: `pytest tests/ -q` → 270 passed, 1 skipped; focused routing/Agent Loop tests pass; frontend production build and local HTTP smoke checks pass. Git writes remain delegated to Claude.
