# OpenAgent Studio

一个本地优先、以可拖拽工作流画布为主体的 OpenCode + Agent Harness 创作工具。

当前实现包含：

- 版本化的 `ProjectSpec`，统一描述 Agent、Provider、Harness 和 Workflow；
- Pydantic 校验和基于 ETag 的并发写保护；
- 编译为 OpenCode 配置和 Agent Harness manifest；
- React + TypeScript + React Flow 正规前端；
- 画布式节点拖放、连线、属性编辑、缩放和小地图；
- OpenCode 驱动的 AI 对话生成器，逐节点增量生成并实时渲染画布；
- Workflow 节点的基础持久化模型（执行器将在后续版本接入 Harness）。

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

默认不会监听公网地址。`project.yaml` 是规范源文件，生成的 OpenCode/Harness
配置应在真正部署流程中写入目标项目；MVP 的 `/api/compile/*` 端点先返回编译结果，
避免意外覆盖用户文件。

仓库中的 `project.yaml` 是可直接打开的中文示例，包含回声服务和代码任务两个智能体。

Studio 启动时会自动检查并启动 `/Users/ypc/agent-harness`，无需另开终端。
“运行方式”页面可以直接准备环境、启动、停止和重启智能体。若 Harness 安装在其他
位置，可通过 `AGENT_HARNESS_ROOT` 指定目录；设置 `OPENAGENT_START_HARNESS=0`
可以关闭自动启动。

OpenCode 生成器使用本机 `opencode run --format json`。可通过
`OPENCODE_BIN` 指定程序，使用 `OPENCODE_GENERATOR_MODEL` 指定生成模型；未设置时
使用项目中的首个模型。OpenCode 只负责理解需求和生成工作流，不负责实际运行流程。

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

## 安全边界

这是本地控制面板，不提供远程认证。API key 只能通过环境变量名引用（例如
`api_key_env: KAX_API_KEY`），不要把 secret 写入 `ProjectSpec` 或前端。部署到团队或
远程环境前，需要补充认证、RBAC、审计和真正的进程/工具沙箱；Harness 当前的
`allow/ask/deny` 仍然是声明式策略，不是 OS 级强制隔离。

## 示例规范

```yaml
version: "1"
name: coding-agents
agents:
  - id: builder
    name: Builder
    model: kax/grok-4.5
    mode: primary
    prompt: You implement and verify changes.
    max_steps: 30
    permission: {edit: allow, bash: ask, webfetch: ask}
providers:
  - id: kax
    npm: '@ai-sdk/openai-compatible'
    base_url: https://example.invalid/v1
    api_key_env: KAX_API_KEY
harness:
  - id: builder
    name: Builder runtime
    cwd: ../my-agent
    task:
      command: [uv, run, python, worker.py]
      verification: [{name: tests, command: [uv, run, pytest]}]
```
