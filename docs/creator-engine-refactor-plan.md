# Creator Harness 底层生成引擎重构计划

## Context（为什么改）

当前底层 `GeneratorManager`（`openagent_studio/generator.py`）的生成核心是三套**服务端硬编码状态机**：

- `_build_creation()` — 创建：`while True` 每轮只许一个 `add_node`，`finish_creation` 结束
- `_build_incrementally()` — 修改：`while True` 每轮只许一个 `add_node`/`update_node`/`delete_node`
- `_build_direct()` — 直出：一次吐完整 `WorkflowSpec`（本会话已加）

痛点：**「每轮只许一个节点」这个硬约束太死**，模型没有全局视野，反复进出循环，效果差、token 浪费、容易停滞。

对照四个开源项目的底层（均已核实源码）：

| 项目 | 底层机制 | 一句话提炼 |
|------|----------|-----------|
| WorkflowAgent | Agent + 工具调用（LLM 自主建图） | **模型驱动**，服务端只校验兜底 |
| AutoFlow 2.0 | 意图 → 结构化蓝图 → 确定性编译 | **一次吐蓝图** + 确定性编译 |
| Maestro-Flow | 意图 → 40+ 命令链 + Ralph 决策引擎 | **语义宏操作** + 结果驱动决策（继续/回退/修复） |
| Marktoflow | 声明式产物 + 自校正 agentic loop | **声明式单一事实源** + 自愈循环 |

**共性结论**：没有任何一家用「服务端硬编码单节点状态机」。它们要么模型自主建图（WorkflowAgent），要么整图直出（AutoFlow），要么用「命令链」这种比单节点更粗的语义宏操作（Maestro-Flow）。

## 目标架构

把三套 build 路径收敛为一个**模型驱动的 agentic 引擎**，四种优势全部落入一个可插拔框架：

```
用户自然语言
   ↓
IntentParser 分类意图                        ← Maestro-Flow（已有，保留）
   ↓
选择 ActionMode（蓝图 / 工具调用 / 命令链）   ← 策略选择（新增）
   ↓
【Agentic 循环】                              ← WorkflowAgent + Marktoflow
   LLM 产出「操作序列」或「完整蓝图」
   ↓ 每步
   确定性校验 + 应用 + 回填结果               ← AutoFlow（复用现有 _normalize/_validate）
   ↓ 失败
   反馈证据 → 模型自愈重试                    ← Maestro-Flow 决策引擎（复用现有 repair）
   ↓ 成功
运行时验收（真实跑 + 语义判定）              ← 复用现有 WorkflowEvaluator
   ↓
finalize → 保存 → SSE 推前端                 ← 复用现有 _finalize
```

核心变化只有一处：**循环的控制权从「服务端硬编码」移到「模型 + 命令链策略」**，其余校验/验收/保存全部复用，保证兼容。

## 四种优势的落点

1. **WorkflowAgent（模型自主建图）** → 新增 `ActionMode.TOOLCALLS`：给模型一组建图操作（add_node/update_node/delete_node/connect/disconnect/finalize），让它在一次 agentic 会话里**自主决定调用哪些、调用几个**，服务端逐步校验+应用+回填结果，直到模型输出 `finalize`。这是对现有「单 JSON 结果」协议的自然扩展。

2. **AutoFlow（蓝图直出）** → 已有 `_build_direct`，保留为 `ActionMode.BLUEPRINT`。

3. **Maestro-Flow（命令链）** → 新增「命令链目录」：把高频语义操作（如「添加审批分支」= add approval + 两条 condition 边 + 重连下游）定义为**命名宏操作**，模型按链名调用，而不是逐个裸节点。决策引擎（继续/回退/修复）复用现有 `_BuildProgressGuard` + `_result_feedback`。

4. **Marktoflow（声明式 + 自愈循环）** → `WorkflowSpec` JSON 已是单一事实源；把「生成过程」与「最终产物」彻底解耦——引擎只对 `WorkflowSpec` 读写，循环就是自愈循环。

## 兼容性契约（硬约束，绝不能破坏）

替换底层，但**对外契约零变更**：

| 契约面 | 必须保持 | 位置 |
|--------|----------|------|
| 前端 API | `sendCreatorDecide` / `sendCreatorGenerate` / SSE `stream_events` / `cancel` / `list_generations` / `chat-status` / `messages` | `frontend/src/api.ts`、`creator/routes.py` |
| 数据模型 | `WorkflowSpec` / `WorkflowNode` / `WorkflowEdge` / `WorkflowEvaluation` 字段不变 | `models.py` |
| 静态校验 | `validate_executable_workflow` 签名与语义不变 | `workflow_runner.py` |
| 运行时验收 | `WorkflowEvaluator.evaluate` / `CandidateResult` 不变 | `evaluation.py` |
| SSE 事件名 | `generation.started/step/stage/preview/completed/failed/stalled` 等名称与 data 形状不变 | 前端依赖这些 |
| 公共方法 | `GeneratorManager.create/create_direct/start/optimize/resume/cancel/require/stream_events/get_messages` 签名不变 | `generator.py` |

## 双 Harness 兼容性边界（重要）

本项目有**两个完全不同的「Harness」**，必须严格区分，重构只碰其中一个：

| | 通用 Harness（agent-harness） | Creator Harness |
|---|---|---|
| 位置 | 外部项目 `D:\Projects\my-harness`，运行在 `http://127.0.0.1:8765` | 本项目 `openagent_studio/creator/` |
| 职责 | 任务执行、Agent 治理、可靠性管理 | 工作流创作意图、节点编排、能力注册 |
| 通信 | `agent_harness_sdk` + HTTP `AGENT_HARNESS_URL` | 内部调用，不经网络 |
| 本重构是否修改 | **否** | **是（仅内部生成引擎）** |

### 结论：通用 Harness 完全不需要修改

本重构的改动范围全部落在「OpenCode CLI 生成层 + Creator Harness 创作层」，与通用 Harness 的协议无关：

1. **生成阶段**（create/modify/optimize）用 `opencode run` 子进程（`generator.py:_invoke`），不经过通用 Harness。
2. **运行时验收阶段**用 `WorkflowEvaluator` → 通用 Harness 启动任务（`AGENT_HARNESS_URL`），这个接口**已存在且不变**，本重构不新增、不改动任何通用 Harness 的调用契约。
3. **命令链/工具调用选 agent** 只读 `AgentCapability.capability`（`text-generation`/`repository-analysis`/`test-execution`），该字段由 `AgentCapabilityRegistry.sync_with_general_harness()` 从通用 Harness 的 labels 同步而来——**只读，不反向写入**。

### 需你知悉的两个「非代码」兼容点（不改通用 Harness 代码）

1. **能力标签准确性**：命令链/工具调用会更高频地组合使用 6 种 `requires_agent` 节点。若某个 agent 在 `project.yaml` 的 `harness[].labels["agent-harness/capability"]` 标注不准确，会导致模型选错 agent。这是**配置问题**（改 `project.yaml`），不是通用 Harness 代码问题。
2. **两个 agent 体系不可混**：
   - `opencode.json` 里的 `agent.openagent-*` = **OpenCode CLI 的本地 agent 定义**（生成器/运行时执行器），本重构会改其中 `openagent-generator` 的 prompt。
   - `project.yaml` 的 `harness[]` = **通用 Harness 注册的 agent**（coding/knowledge/test-runner），本重构**不碰**。
   - 两者同名概念但完全独立，改造时严禁混淆。

## 分阶段实施（每阶段独立可交付、可测试、可回滚）

### Phase 0 — 已完成
`create_direct()` + `_build_direct()`（蓝图直出，AutoFlow 优势）。create 意图已切到直出。

### Phase 1 — 命令链 + 放宽粒度（Maestro-Flow）
- 新增 `openagent_studio/creator/chains.py`：命令链目录，把语义宏操作映射到「多节点 + 连线」的原子序列。
- `_build_incrementally` 的 prompt 从「每轮一个节点」改为「每轮一个**命令链/编辑批次**」，一个 chain 可产出多个关联 node+edge，一次应用+一次验证。
- 复用 `_apply_incremental_step` 的校验，但把「单节点」断言放宽为「本链内闭合」。
- **不改任何对外契约**，modify/optimize 立即受益。

### Phase 2 — 工具调用模式（WorkflowAgent）
- 新增 `ActionMode.TOOLCALLS` 与 `_build_toolcalls()`。
- 定义「图工具协议」：模型输出 `<tool name="add_node">…</tool>` / `<tool name="finalize"/>` 序列；服务端逐个校验+应用+回填结果（含校验错误/运行时错误），模型看到结果后决定下一步。
- 关键约束：**图工具是协议，不是真实文件/网络工具**——模型仍不能碰文件系统，保持现有 `openagent-generator` 的 sandbox（`permission: deny`）。
- 新增 `openagent.json` 里的 agent 配置仅调整 prompt（允许多轮、允许输出工具序列），不开放任何真实工具权限。

### Phase 3 — 意图路由 + 配置开关
- `CreatorHarness` / `WorkflowGenerator` 按意图 + 环境变量路由模式：
  - create/modify/repair/optimize → `AGENT_LOOP`（默认）
  - `blueprint|toolcalls|chain|incremental` 仅显式配置时使用
- 环境变量 `OPENAGENT_GENERATOR_MODE`（`agent_loop|blueprint|toolcalls|chain|incremental`）做全局默认，legacy 模式保留为逃生舱。
- 生成 API 与 `sendCreatorDecide` 契约保持不变；前端仅增加 Agent Loop 事件日志和 `ask_user` 回答 UI。

### Phase 4 — （可选）真·函数调用
- 若 OpenCode 部署支持 tool definitions，把「图工具协议」升级为原生 function calling（`opencode.json` 开工具 + 服务端注册 tool handler）。这是纯增量，Phase 2 的协议层已为此预留接口。

## 需修改/新增的文件

| 文件 | 动作 |
|------|------|
| `openagent_studio/creator/chains.py` | 新增：命令链目录 + 链到节点序列的映射 |
| `openagent_studio/generator.py` | 改：抽出共享校验/应用/验收助手；新增 `_build_toolcalls`；放宽 `_build_incrementally` 粒度；`ActionMode` 枚举 |
| `openagent_studio/creator/generator.py` | 改：意图 → 模式路由 |
| `openagent_studio/creator/harness.py` | 改：读 `OPENAGENT_GENERATOR_MODE`，传 mode 给 generator |
| `opencode.json` | 改：`openagent-generator` prompt 支持多轮图工具序列（不开放真实工具权限） |
| `tests/test_creator_chains.py` | 新增：命令链映射测试 |
| `tests/test_creator_toolcalls.py` | 新增：工具调用循环测试 |
| 既有 `tests/test_creator_*.py` | 改：Fake 补充新方法；回归断言 |

## 验证方式

1. **单元/集成**：`pytest tests/test_creator_*.py tests/test_studio.py`——既有的 110+ creator 测试 + studio 测试必须全绿（除已记录的 `test_route_isolation` FastAPI 0.141.1 无关失败）。
2. **新功能测试**：
   - 命令链：`test_creator_chains.py` 验证「添加审批分支」链产出 approval+condition+重连。
   - 工具调用：`test_creator_toolcalls.py` 用 mock `_invoke_result` 侧写「多轮工具调用 → finalize」循环，断言最终 `WorkflowSpec` 正确、失败回填触发重试。
3. **端到端运行时**：重启后端（`start-studio.ps1` 自动杀 8787），前端发「创建一个代码审查流程，先分析代码，再人工审批，最后运行测试」，走 SSE 观察事件流，确认 create 直出 + modify 工具调用都能出图、能验收。
4. **回滚验证**：`OPENAGENT_GENERATOR_MODE=incremental` 启动，确认 legacy 路径仍可用（逃生舱）。

## 关键决策点（需你确认后开工）

1. **工具调用是否要真·function calling，还是先用「协议式」多轮结构化输出？** 建议先协议式（Phase 2，零真实权限风险），真 function calling 留 Phase 4 可选。
2. **modify 默认走 `TOOLCALLS` 还是先走 `CHAIN`？** 建议 TOOLCALLS 为主、CHAIN 为降级，`incremental` 为逃生舱。
3. **是否保留 `incremental` 逃生舱？** 建议保留（环境变量可切回），降低替换风险。

## 实施状态（2026-08-13）

- **Phase 0** ✅ 已完成（`create_direct` 蓝图直出）
- **Phase 1** ✅ 已完成 — `creator/chains.py` 命令链目录 + `_apply_incremental_step` 支持 `nodes` 批次 + `INCREMENTAL_STEP_PROMPT` 放宽粒度
- **Phase 2** ✅ 已完成 — `TOOLCALLS_PROMPT` + `_build_toolcalls()`（多操作有序数组 + 逐操作校验/回填）+ `_coerce_action_list`
- **Phase 3** ✅ 已完成 — `Generation.build_mode` 字段 + `_modify_build_mode()`（读 `OPENAGENT_GENERATOR_MODE`）+ `_run` 分发（blueprint/toolcalls/creation/incremental）
- **Phase 4** ✅ 完成 — Agent Loop 默认控制 create/modify/repair/optimize；DAG 校验、显式 evaluate/finalize、ask_user/resume、超时自动重试已接入，需端到端运行时验证

### 已决策（按建议默认）
1. 工具调用先协议式（非真 function calling）
2. 所有生成意图默认 `agent_loop`；旧 `toolcalls`/`chain`/`incremental` 为降级/逃生舱
3. 保留 `incremental` 逃生舱（`OPENAGENT_GENERATOR_MODE=incremental`）

### 当前默认路由
- create → `agent_loop`（`create_direct` 保留公共兼容名，内部进入 `_build_agent_loop`）
- modify/repair → `agent_loop`（`start` → `_build_agent_loop`）
- optimize → `agent_loop`（`optimize` → `start(optimize_only=True)` → `_build_agent_loop`）
