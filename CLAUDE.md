# OpenAgent Studio — 项目规则

## Harness 本地化约定（最高优先）

通用 Harness 的运行目录已本地化到项目内 **`./my-harness/`**，不再是外部 `D:\Projects\my-harness`。

- **只改、只使用 `./my-harness/`**。任何 Harness 相关改动、重装、重启都针对项目内这一份，绝不操作外部 `D:\Projects\my-harness`。
- 所有脚本（`scripts/start-*.ps1`、`scripts/*.sh`）默认 `HarnessRoot` 已指向 `./my-harness/`，无需再传 `-HarnessRoot`。
- `my-harness/` 是**安装运行目录**（非源码），已被 `.gitignore` 忽略。它包含密钥 `.runtime.env`、安装的包 `.venv/`、运行时状态 `state/`，均由脚本重建，勿手改、勿提交。

### 两个 Harness，严格区分

| | 通用 Harness（agent-harness） | Creator Harness |
|---|---|---|
| 运行目录 | `./my-harness/`（端口 8765） | `openagent_studio/creator/` |
| 职责 | 任务执行、Agent 治理、可靠性 | 工作流创作、意图、能力注册 |
| 源码 | GitHub `xuanf0v0/my-harness`（pinned ref，见 `scripts/harness-version.ps1`），**本地无源码，不改其安装包** | 本项目内，可改 |
| 唯一本地可改的执行器 | `adapters/opencode/`（opencode adapter） | — |

改 `adapters/opencode/` 后，必须 `scripts/install-harness.ps1` 重装进 `./my-harness/.venv` 并重启 harness（8765）才生效。

**只有改了 `adapters/opencode/` 源码才需要 `-ForceInstall` 重装 harness。** 改模型/智能体配置（`opencode.json`、`project.yaml`、`scripts/register-harness-agent.*`）或后端/前端代码（`openagent_studio/`、`frontend/`）时，直接 `scripts/start-all.ps1`（**不带 `-ForceInstall`**）重启即可——harness 已是 pinned ref，脚本会跳过安装。误用 `-ForceInstall` 会在运行中的 harness 进程锁住 `.exe` 时触发「拒绝访问 (os error 5)」，且会因网络抖动拉取 GitHub 失败。

## 生成引擎模式（`OPENAGENT_GENERATOR_MODE`）

| 模式 | 用途 |
|------|------|
| `blueprint` | 一次直出完整工作流（create 默认） |
| `toolcalls` | 模型自主规划多个图操作、逐个校验（modify/repair 默认） |
| `chain` / `incremental` | 命令链增量（逃生舱） |

## 关键路径

- 后端：`openagent_studio/app.py`（端口 8787）
- 生成引擎：`openagent_studio/generator.py`
- Creator Harness：`openagent_studio/creator/`
- opencode adapter：`adapters/opencode/`
- 前端：`frontend/`（Vite，端口 5173）
- 一键启动：`scripts/start-all.ps1`

## 验证

- 测试：`.venv/Scripts/python -m pytest tests/`
- 已知无关失败：`tests/test_studio.py::test_route_isolation_create_entry_and_existing_message_route`（FastAPI 0.141.1 的 `_IncludedRouter` 无 `.path` 属性，非本项目 bug）

<!-- AgentBridge:start -->
## AgentBridge — Multi-Agent Collaboration

You are working in a **multi-agent environment** powered by AgentBridge.
Another AI agent (Codex, by OpenAI) is available in a parallel session on this machine.

### Communication mechanism
- **Claude → Codex**: Use the AgentBridge MCP tools (`reply` / `get_messages`) — these are yours only.
- **Codex → Claude**: Codex has no symmetric tool. The bridge transparently intercepts Codex's normal output and forwards it to you as push notifications (if a push fails, drain the fallback queue with `get_messages`).
- If Codex ever complains it can't find a "send-to-Claude" API, remind it that its side is transparent — it just writes a reply and you'll see it.

### When to collaborate vs. work solo
- **Collaborate** when the task benefits from a second perspective, parallel execution, or capabilities you lack (e.g., sandboxed code execution, independent verification).
- **Work solo** for simple, self-contained tasks where the coordination overhead isn't worth it.
- When in doubt, **propose a task split** to Codex rather than doing everything yourself.

### Capability comparison
| Capability | Claude (you) | Codex |
|---|---|---|
| Architecture & planning | Strong | Moderate |
| Code review & analysis | Strong | Strong |
| Sandboxed code execution | No | Yes |
| File editing & refactoring | Yes (via tools) | Yes (via sandbox) |
| Web search & docs | Yes | Limited |
| Independent verification | Cross-review | Reproduce & test |

### How to start collaborating
1. When you receive a complex task, **proactively propose a division of labor** to Codex via the reply tool.
2. State what you'll handle and what you'd like Codex to take on.
3. Ask for Codex's agreement or counter-proposal before proceeding.
4. After task completion, **cross-review** each other's work.

### Budget pacing — drive the WEEKLY quota to ~100% over the week, evenly, without reaching a 5h cap (active when agent-quota-guard is installed)
- **Core principle: token is the means, value is the end.** Raising intensity means producing more real parallel value (deeper reviews, more independent exploration / verification / genuine subtasks) — never manufacturing low-value work to consume quota. The budget to MAXIMIZE is the **weekly** quota (refreshed once a week): drive each side's weekly toward ~100% by its weekly reset, and consume it **evenly** across the week — front-loading then starving, or under-consuming throughout, both leave weekly quota unredeemed (forfeited). The **5h window is NOT a quota bucket to fill — it is a RATE CAP**: stay under it within any 5h period; reaching it = a forced pause until the 5h resets = wasted time, not progress.
- **Re-query your budget before EVERY allocation decision** — Claude: `get_budget` → **rendered text** covering both sides; Codex: `check_budget` with `agent:"claude"|"codex"` → **normalized JSON**, per side. (Two different shapes — read the right one below.) Never reuse remembered numbers: a weekly window can refresh EARLY (resetting both 5h and weekly), fully restoring a side you believed was exhausted.
- **Even-pacing test (per side — Claude runs it)** — compare two quantities: *budget-windows* = how many 5h windows the weekly quota still covers at the current burn rate; *clock-windows* = how many 5h windows physically fit before the weekly reset = (weekly reset − now) ÷ 5h. **Claude** (`get_budget` text) carries BOTH, pre-computed for BOTH sides: the lines "按当前节奏，周额度还够 … 个 5h 窗口" (budget-windows) and "距周刷新还能容纳 … 个 5h 窗口（时钟）" (clock-windows). **Codex** (`check_budget` JSON) today carries only per-bucket `util` / `reset_epoch` / `reset_after_seconds` — no burn rate, no `five_hour_windows_left` — so Codex CANNOT compute budget-windows itself; it reads its weekly `util` and clock-windows only. To locate Codex's weekly bucket: of the `buckets[]` entries whose `id` contains `seven_day` or `secondary_window` (there can be several — e.g. a model-specific `additional_rate_limits[…]` one at 0%), take the HIGHEST-util one (the binding account-level window, matching how the bridge parses it); its clock-windows = `reset_after_seconds` ÷ 5h (never the top-level `reset_epoch`, which tracks the current limiter, not necessarily the weekly window). For the budget-windows half and the raise/hold/reduce verdict, Codex relies on Claude's `get_budget` (the burn projection lives there, for both sides) and reports its own weekly `util` + reset timing so Claude can run the test. (If a future `check_budget` exposes `five_hour_windows_left` on the weekly bucket, Codex reads it directly.) **The verdict (Claude computes it, per side):** budget > clock → **under-consuming** (weekly will be left unused) → **raise intensity**; budget < clock → **over-consuming** (won't last to the weekly reset) → **reduce intensity**; within ~1 window, or no confident rate → **hold**. **Codex, absent a fresh Claude verdict, holds at its current intensity (it never escalates unilaterally) and stays clear of the 5h cap — surfacing its weekly `util` + reset timing so Claude can issue the verdict.**
- **Raise intensity — use the levers your role has.** Orchestrator (Claude): pick larger, more-decomposable tasks; run more parallel subagents at once (3–5+ vs 1); raise delegation density; open more concurrent streams (review + explore + verify in parallel). Executor (Codex): go deeper in-turn, take larger chunks, run more verification/repro. Both: deepen quality (multi-angle review, broader test/repro) — never manufacture make-work. **Reduce intensity:** fewer/serial subagents (Claude), short bounded chunks, defer optional deep work. Stay below the **动态暂停线** (shown in `get_budget`; its `余量` = headroom from your current util to that soft line, measured on the resettable hard-winner window — the 5h OR the weekly window, whichever currently limits you) — that soft ceiling, not the raw 5h cap, is the "do not cross, avoid a forced pause" line. **If that line is absent, or you only have JSON (Codex),** fall back to the 5h bucket's raw util vs 100% (Codex: of the `buckets[]` entries whose `id` contains `five_hour` or `primary_window`, take the HIGHEST-util one) and keep clear of the 5h cap.
- **Distinguish 5h from weekly:** a 5h window resetting does NOT consume or waste weekly budget — it only refreshes your rate headroom, so you can keep going when weekly is under-consumed. A near 5h reset is therefore not urgency but the release of a rate limit. The real "unused = forfeited" is the **weekly budget as its WEEKLY reset nears**: if weekly is still under-consumed then, raise intensity (within the 5h cap) to use it. If even pacing needs a rate beyond one 5h window's capacity, you are rate-limited → keep each 5h window as full as possible (under the cap).
- **Two-subscription imbalance — the quotas are INDEPENDENT and differ in BOTH amount AND reset timing** (each side's weekly and 5h windows reset on different clocks). **The cross-side split is the orchestrator's (Claude) decision:** route more work to the side that is MORE under-consuming on the even-pacing test (the larger budget-windows − clock-windows gap); when EITHER side lacks a confident rate (so the gap can't be compared), fall back to the more budget-rich side (larger absolute weekly headroom). On any tie (equal gap, or equal headroom), prefer the side whose **weekly resets SOONER** (its leftover is forfeited earlier). **As the executor (Codex) you do NOT decide the global split** — execute what you're assigned, and when your own budget is rich report it (with evidence) so Claude routes more to you. The tighter / over-consuming side carries less.
- **Side-aware pause (the hard floor the code enforces — obey, do not reinvent), with each side's own action:** **Codex exhausted** (`system_budget_pause`) → Codex's turns stop (gate closed); **Claude** must not retry replies and continues solo on independent work, checkpointing the split point — but the SAME `system_budget_pause` is ALSO emitted when both sides are exhausted, so do not infer "solo" from the directive name alone: read its content (it names the paused side[s]) or re-check `get_budget`, and continue solo ONLY while Claude's own side is healthy; if Claude is also at its line, handle it as **Both** below. **Claude exhausted** (`system_budget_handoff`) → **Claude** sends ONE handoff (remaining tasks / context / artifact locations / acceptance criteria) then stops; **Codex** receives the baton and carries the work forward as far as its remaining quota allows that turn. **Both** → joint pause; checkpoint and wait for `resume` (Claude's own quota-guard also hard-stops Claude independently). A transient probe **429 is NOT exhaustion** → fall back to cached util and keep working.
<!-- AgentBridge:end -->
