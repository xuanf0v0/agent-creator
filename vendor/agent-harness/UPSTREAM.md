# Vendored Agent Harness

- Upstream: `git@github.com:xuanf0v0/my-harness.git`
- Branch: `main`
- Base revision: `474f8a3fc2598fa30f086fc9ddb1f736a3a32364`
- Snapshot: upstream local working tree, including the uncommitted protocol,
  deployment, health-check, plugin and sandbox-backend changes present at import
- Imported: 2026-08-04

The copy excludes upstream Git metadata, virtual environments, caches, runtime
state and local `openagent-agents/` files. It does not modify the source working
tree. OpenAgent adds only
`src/agent_harness/__main__.py`, a dependency-free source-tree entry point for
the `serve` and `validate` commands used by the integration.
