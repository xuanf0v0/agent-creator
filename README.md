# OpenAgent Studio

一个本地优先、以可拖拽工作流画布为主体的 OpenCode + Agent Harness 创作工具。

当前实现包含：

- 版本化的 `ProjectSpec`，统一描述 Agent、Provider、Harness 和 Workflow；
- Pydantic 校验和基于 ETag 的并发写保护；
- 编译为 OpenCode 配置和 Agent Harness manifest；
- React + TypeScript + React Flow 正规前端；
- 画布式节点拖放、连线、属性编辑、缩放和小地图；
- OpenCode 驱动的 AI 对话生成器，逐节点增量生成并实时渲染画布；
- 接入 Agent Harness 的 Workflow 执行器，支持任务型和服务型 Agent；
- 条件分支、并行调度、有限循环、人工审批、验证器、输出聚合和运行取消；
- 基于 SSE 的实时节点状态、Harness 任务进度和运行事件面板。

## 启动

```bash
uv venv
uv pip install -e '.[dev]'
source .venv/bin/activate
OPENAGENT_SPEC=project.yaml openagent-studio
```

如果不想激活虚拟环境，可以直接调用项目内的入口：

```bash
OPENAGENT_SPEC=project.yaml .venv/bin/openagent-studio
```

已经安装依赖且不希望 `uv` 重新解析依赖时，也可以使用
`uv run --no-sync openagent-studio`。

打开 <http://127.0.0.1:8787>。也可以直接运行：

```bash
uv run uvicorn openagent_studio.app:app --host 127.0.0.1 --port 8787
```

默认不会监听公网地址。`project.yaml` 是规范源文件；`/api/compile/*` 端点可以预览
生成结果。Studio 启动时会把 Harness 配置原子同步到项目内的专用目录
`.openagent-agents`，不会覆盖 Harness 自带的 `agents` 目录。

仓库中的 `project.yaml` 已配置真实的 `deepseek/deepseek-v4-flash` OpenCode 代码智能体，以及一套
可直接运行的选品决策工作流；不包含回声、固定响应或无模型 worker。选品流程中的需求整理、市场研究、
竞品分析和利润评估节点都配置了独立的角色、目标、上下文、约束与输出格式提示词。

Studio 启动时会自动检查并启动 `/Users/ypc/agent-harness`，无需另开终端。若 Harness
已在 `AGENT_HARNESS_URL` 运行，Studio 会直接复用它。工作流中的任务型 Agent 会通过
Harness 的 `/api/tasks` 创建受治理任务、轮询终态并读取日志；服务型 Agent 会在需要时
自动 setup/start，再通过 Harness 的 service proxy 调用。

相关环境变量：

- `AGENT_HARNESS_ROOT`：Harness 安装目录，默认 `/Users/ypc/agent-harness`；
- `AGENT_HARNESS_BIN`：`agent-harness` 可执行文件；
- `AGENT_HARNESS_MANIFESTS`：Studio 管理的 manifest 目录；
- `AGENT_HARNESS_URL`：Harness API 地址，默认 `http://127.0.0.1:8765`；
- `OPENAGENT_START_HARNESS=0`：不由 Studio 自动启动 Harness；
- `OPENAGENT_SYNC_HARNESS=0`：不把项目中的 Harness 配置同步到 manifest 目录。

Studio 保存完整项目配置时也会刷新这些文件；由于 Harness 启动时一次性加载 catalog，
修改 `harness` 配置后需要重启 Studio/Harness 才会应用。仅编辑 Workflow 不需要重启。

Harness 会限制 Agent 工作目录不能逃出 manifest 目录的父目录。因此默认配置下，
`harness[].cwd` 应位于当前项目内；自定义 `AGENT_HARNESS_MANIFESTS` 时，也要保证其
父目录能够合法包含 Agent 工作目录。

OpenCode 生成器使用本机 `opencode run --format json`。可通过
`OPENCODE_BIN` 指定程序，使用 `OPENCODE_GENERATOR_MODEL` 指定生成模型；未设置时
使用项目中的首个模型；如果两者都没有配置，生成请求会直接失败，不会降级到替身模型。
当前项目的生成对话和 Harness 代码任务都明确使用 `deepseek/deepseek-v4-flash`。前者负责理解需求和
生成工作流，后者通过 `openagent_studio.harness_opencode` 执行实际 Agent 任务。
页面首次打开会显示 OpenCode 创作对话框；关闭后可通过顶部“OpenCode 创建”按钮或
右侧“AI 创建”页签重新打开。对话中的节点变更会通过 SSE 实时反映到画布并保存。
生成器会把可用 Harness Agent 清单交给模型，并要求每个节点同时生成执行参数：Agent
必须包含 `agent_id` 和完整 `prompt`，提示词/循环/输出节点包含 `template`，条件节点包含
`expression` 与分支条件。后端会拒绝不存在的 Agent，并为遗漏的可选参数补充可执行默认值。
右侧标题会显示实际模型和凭证状态。当前项目从服务端文件
`/Users/ypc/agent-manager/agents/listing-optimization/.env` 读取 `OPENAI_API_KEY`，密钥不会
进入浏览器或项目 YAML；读取失败时请求会明确报错，不会切换到其他模型或演示响应。

## 工作流执行

点击右上角“运行”，或在右侧“运行”页输入任务后启动。前端会先保存当前画布，每次执行
都会生成独立的 run 并对已保存配置做快照；后续编辑不会改变正在运行的任务。运行记录当前保存在 Studio
进程内存中，重启后不会保留。

节点按 Dify/n8n 风格分组，所有节点都有持久化参数和执行语义：

左侧节点库支持按中文名称、节点类型、分组和能力说明搜索，分组可以折叠，便于在完整节点集合中快速定位。

- 触发器：`manual_trigger`、`webhook`、`schedule`；Webhook 使用配置的 `/hooks/...`
  路径启动工作流，Schedule 支持带范围、列表和步长的五段 Cron、字段范围校验、IANA 时区和分钟级去重；
- AI：`llm`、`agent`、`tool`、`code` 均通过 Harness task/service 执行，
  `knowledge_retrieval` 支持查询模板、内置文档和 Top-K 召回；
- 数据处理：`prompt`、`variable_set`、`transform`、`merge`，支持 JSON 解析/序列化、
  路径提取、字段筛选、数组扁平化以及 array/object/concat 合并；
- 集成：`http_request` 支持方法、请求头、模板化请求体、超时和状态码策略，默认禁止
  非 HTTPS 及私网/回环/保留地址，降低 SSRF 风险；
- 流程控制：`condition`、`switch`、`parallel`、`iteration`、`loop`、`delay`；图本身
  保持无环，迭代/循环限制为 1–100 次，等待限制为 300 秒；
- 人工与质量：`approval`、`validator`；
- 编排与输出：`subworkflow` 支持最多 10 层嵌套，`output` 定义最终结果。

除触发器与审批外，节点都支持 0–5 次重试、重试间隔、失败即终止或使用兜底值继续。
多个触发器可共存，但每次运行只激活实际命中的入口，避免 Webhook、Cron 和手动入口串跑。

模板支持 `{{input}}`、`{{latest}}` 和 `{{nodes.节点ID}}`。条件表达式支持路径真值判断，
以及 `==`、`!=`、`contains`，例如 `latest.task.status == "completed"`。连线条件留空表示
始终执行；条件节点的分支通常分别填写 `true` 和 `false`。

Agent 节点会使用 `openagent-{run_id}-{node_id}` 作为 Harness 幂等键。取消工作流时，
执行器会唤醒等待中的审批节点，并向所有活跃 Harness task 发送取消请求。

## 前端开发

前端源码位于 `frontend/`，生产构建由 FastAPI 直接托管：

```bash
cd frontend
npm install
npm run build
```

开发时可以运行 `npm run dev`，Vite 会把 `/api` 请求转发到 8787 端口的 FastAPI。

## API

- `GET /api/spec`：返回规范和 ETag
- `PUT /api/spec`：校验并原子保存规范，使用 `If-Match` 防止覆盖并发修改
- `GET /api/compile/opencode`：生成 OpenCode JSON 配置
- `GET /api/compile/harness/{agent_id}`：生成 Harness YAML manifest
- `POST /api/workflows/{workflow_id}/runs`：启动工作流，body 可包含 `input` 和 `relative_path`
- `GET|POST|PUT|PATCH|DELETE /hooks/{path}`：触发 path 匹配的 Webhook 工作流
- `GET /api/workflow-runs?workflow_id=...`：列出运行记录
- `GET /api/workflow-runs/{run_id}`：读取运行和节点状态
- `GET /api/workflow-runs/{run_id}/events`：订阅 SSE 运行事件
- `POST /api/workflow-runs/{run_id}/cancel`：取消运行及活跃 Harness task
- `POST /api/workflow-runs/{run_id}/nodes/{node_id}/approval`：提交人工审批决定

## 安全边界

这是本地控制面板，不提供远程认证。API key 只能通过环境变量名引用（例如
`api_key_env: OPENAI_API_KEY`），不要把 secret 写入 `ProjectSpec` 或前端。部署到团队或
远程环境前，需要补充认证、RBAC 和审计。Studio 不会直接执行 Workflow 中提供的任意
shell 命令；Agent 与验证任务都必须经过 Harness。实际隔离强度由 Harness runtime 和
操作系统环境决定，`allow/ask/deny` 工具策略本身不是 OS 级沙箱。

## 示例规范

```yaml
version: "1"
name: coding-agents
agents:
  - id: builder
    name: Builder
    model: deepseek/deepseek-v4-flash
    mode: primary
    prompt: You implement and verify changes.
    max_steps: 30
    permission: {edit: allow, bash: ask, webfetch: ask}
providers:
  - id: deepseek
    npm: '@ai-sdk/openai-compatible'
    base_url: https://api.deepseek.com
    api_key_env: OPENAI_API_KEY
    env_file: /path/to/server-only/.env
harness:
  - id: builder
    name: Builder runtime
    cwd: ../my-agent
    task:
      command: [.venv/bin/python, -m, openagent_studio.harness_opencode, --model, deepseek/deepseek-v4-flash, --agent, build, --env-file, /path/to/server-only/.env]
      verification: [{name: tests, command: [uv, run, pytest]}]
workflows:
  - id: build-and-review
    name: 构建并审批
    nodes:
      - id: task
        type: agent
        data: {agent_id: builder, prompt: "{{input}}"}
      - id: approve
        type: approval
        data: {description: 请确认 Harness 验证结果}
      - id: result
        type: output
        data: {}
    edges:
      - {source: task, target: approve}
      - {source: approve, target: result}
```
