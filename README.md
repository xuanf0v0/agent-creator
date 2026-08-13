# OpenAgent Studio

一个本地优先、以可拖拽工作流画布为主体的 OpenCode + Agent Harness 创作工具。

当前实现包含：

- 版本化的 `ProjectSpec`，统一描述 Agent、Provider、Harness 和 Workflow；
- Pydantic 校验和基于 ETag 的并发写保护；
- 编译 OpenCode 配置，并通过独立 `agent-harness-sdk` 调用稳定 `/api/v1`；
- React + TypeScript + React Flow 正规前端；
- 画布式节点拖放、连线、属性编辑、缩放和小地图；
- OpenCode 驱动的 AI 对话生成器，按节点和层级增量构建，并只保存真实运行通过的完整工作流；
- 接入独立 my-harness 的 Workflow 执行器，支持任务提交、状态、日志、结果和取消；
- 条件分支、并行调度、有限循环、人工审批、验证器、输出聚合和运行取消；
- 基于 SSE 的实时节点状态、Harness 任务进度和运行事件面板。
- 飞书与 QQ 官方机器人接口，支持验签、防重放、事件去重、工作流路由和自动回复。

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

`openagent-studio` 启动时会自动结束占用 `127.0.0.1:8787` 的旧监听进程，
避免重复启动时报端口冲突。需要保留旧进程时可设置 `OPENAGENT_KILL_PORT=0`。
Studio 不安装、不启动、不监督 Harness，也不会写 Harness manifest 或 Catalog。

打开 <http://127.0.0.1:8787>。也可以直接运行：

```bash
uv run uvicorn openagent_studio.app:app --host 127.0.0.1 --port 8787
```

默认不会监听公网地址。`project.yaml` 是规范源文件；`/api/compile/opencode` 可以预览
OpenCode 配置。Studio 始终连接独立运行的 Harness。

仓库中的 `project.yaml` 已配置真实的 `deepseek/deepseek-v4-flash` OpenCode 代码智能体，默认打开的是
空白工作流画布；不包含回声、固定响应或无模型 worker。你可以直接通过 OpenCode 创作助手从空白画布开始
创建流程，也可以拖入节点后再进行增量修改。飞书和 QQ 集成默认仍引用 `product-selection` 工作流，
需要启用机器人时请先在画布中完成并保存对应流程。

Studio 固定依赖 [xuanf0v0/my-harness](https://github.com/xuanf0v0/my-harness)
提交 `d73a00704b98b343a36369989b299a3f71fc721c` 的独立 Python SDK。所有任务只走
`/api/v1/tasks`、状态、日志、结果和取消接口，不再包含旧 `/api/tasks` 或服务代理兼容路径。
`project.yaml` 的 Harness 项只保存逻辑 ID、`backend_id` 和 Harness Catalog 中的 `agent_id`。
任务客户端还会通过 `/api/v1/task-agents`（仅需 Task Token）检查 Agent 的通用 labels、协议和 setup readiness；
缺失、身份不匹配或 `setup_required` 会在首次 OpenCode 调用前快速失败，不会交给模型修复。

相关环境变量：

- `AGENT_HARNESS_URL`：Harness API 地址，默认 `http://127.0.0.1:8765`；
- `AGENT_HARNESS_TASK_TOKEN`：v1 任务提交、读取和取消使用的 Bearer Token；
- 非默认后端使用 `AGENT_HARNESS_<BACKEND_ID>_URL` 和
  `AGENT_HARNESS_<BACKEND_ID>_TASK_TOKEN`，其中 ID 转为大写下划线形式。

Studio 不读取 `AGENT_HARNESS_MANAGEMENT_TOKEN`。管理 Token 只应存在于 Harness 运维环境中，
由运维脚本通过 `/api/v1/agents` 注册或更新 Catalog。AI 创建会在首次模型调用前通过 SDK 检查
`/api/v1/capabilities`；运行时不可用会快速失败，不会把基础设施故障误当成候选缺陷反复修复。

### 安装并启动独立 Harness（Windows）

```powershell
.\scripts\start-all.ps1
```

`start-all.ps1` 是 Windows 一键入口：检查并按固定提交安装/升级独立 Harness，启动 Harness、
Studio、Vite 开发前端和 n8n，并等待四项健康检查通过。默认地址分别是
`http://127.0.0.1:8765`、`http://127.0.0.1:8787`、`http://127.0.0.1:5173` 和
`http://127.0.0.1:5678`。重复运行只停止能确认属于当前仓库或当前 Compose 项目的旧进程/容器；
未知端口占用会显示 PID 和命令行后中止。n8n 重建不会删除 `n8n_data` 卷、凭证或工作流。
需要强制重装 Harness 时使用 `.\scripts\start-all.ps1 -ForceInstall`。

n8n 需要在项目 `.env` 中配置稳定的 `N8N_ENCRYPTION_KEY`。Studio 调用 n8n 使用
`127.0.0.1:5678`；n8n 容器回调宿主 Studio 必须使用 `host.docker.internal:8787`。
版本化示例位于 `n8n/workflows/`，总启动脚本会导入并发布 `studio-fetch-sheet` 和
`studio-callback-test`。脚本不导入第三方凭证，也不会删除 Docker 卷。

macOS 可使用同等的一键编排入口：

```bash
./scripts/start-studio.sh
```

`start-studio.ps1` 默认会自动启动本机独立 Harness，等待 `/api/v1/capabilities` 协商到 v1 后再启动 Studio；重复执行不会启动第二个 Harness。若启动失败，脚本会直接显示 Harness 原始错误，完整输出保存在 `.harness/start-harness.stdout.log` 和 `.harness/start-harness.stderr.log`。若 Harness 已由其他方式管理，使用 `-SkipHarness` 跳过自动启动。远程 `HarnessUrl` 也不会被本脚本代管。

默认安装目录是 `D:\Projects\my-harness`。它拥有独立虚拟环境、`state` 目录、空 bootstrap
manifest 目录和 `.runtime.env`。`catalog.json` 由 Harness 管理；注册脚本只在运维侧读取管理
Token。`start-studio.ps1` 会把任务 Token 注入 Studio 进程。OpenCode 适配器位于
`adapters/opencode`，是独立可安装包，不导入 `openagent_studio`。

OpenCode 生成器使用本机 `opencode run --format json`。可通过
`OPENCODE_BIN` 指定程序，使用 `OPENCODE_GENERATOR_MODEL` 指定生成模型；未设置时
使用项目中的首个模型；如果两者都没有配置，生成请求会直接失败，不会降级到替身模型。
Windows 下会自动解开 npm 的 `opencode.cmd` shim 并直接调用真实 `opencode.exe`，避免 Harness 任务 prompt 在 cmd 转发时丢失。
当前项目的生成对话和 Harness 代码任务都明确使用 `deepseek/deepseek-v4-flash`。前者负责理解需求和
生成工作流，后者通过独立的 `openagent-harness-opencode` 适配器执行实际 Agent 任务。
页面首次打开会显示 OpenCode 创作对话框；关闭后可通过顶部“OpenCode 创建”按钮或
右侧“AI 创建”页签重新打开。创作助手支持连续对话：询问概念、当前画布、节点作用、设计建议或错误原因时会直接由真实 OpenCode 回答，不修改画布；需求不完整时会先追问，只有明确要求创建、增加、删除、连接、调整或优化工作流时才进入修改流程。

进入创建或修改流程后，生成器会逐层增量构建：一次只提出一个节点或一条变更。输入、模板、变量、转换、合并和输出节点先做静态校验；AI、工具、HTTP、审批、条件、循环、子工作流和条件连线变更还会沿正式 `WorkflowManager → agent-harness-sdk → /api/v1` 路径真实探测当前层。通过后才接受该层并继续下一个节点或分支。收到 `complete` 后生成并锁定验收用例，执行完整工作流验收；完整验收失败时先复验上轮失败用例，通过后再执行一次全量回归。
同一稳定图上连续两次出现同一节点、同类错误或无语义变化时，生成器会发送 `generation.stalled` 并暂停；新增/删除回到历史图状态时立即暂停。失败修复调用若只启动协议但没有返回文本、reasoning、工具或诊断，会在 60 秒终止后使用同一模型自动重试一次；第二次仍超时则发送 `generation.stalled`，并保留两次调用证据。页面以只读方式保留最后通过的内存草稿，可补充要求后通过 `POST /api/generator/generations/{id}/resume` 继续修复。暂停草稿不会写入 `project.yaml`；Studio 重启后草稿失效，页面会明确恢复磁盘中的原工作流。普通规划超时、首次即产生模型活动的非静默超时、Harness 基础设施和不可恢复错误仍直接失败。
验收运行不写入普通运行历史。

生成器通过标准输入以 UTF-8 传递提示词，不依赖 Windows 命令行长度或模型不可见的文件附件。超过
`OPENCODE_COMPACT_PROMPT_LENGTH`（默认 12000 字）的上下文会先由禁用工具的 compaction Agent
无损提炼，提炼默认最多等待 `OPENCODE_COMPACTION_TIMEOUT=30` 秒。提炼超时时会保留原始完整上下文继续生成，
并在当前生成任务中跳过后续提炼；进程成功退出但返回空内容时，会沿同一模型和同一 compaction Agent 严格重试一次，再次为空或其他提炼错误仍会直接终止并把真实错误返回页面；
正式生成继续使用 OpenCode 原生 `plan` 创作助手，不切换到其他兜底 Agent。

右侧“验收标准”可编辑真实运行输入、输出检查、语义目标、审批决策和单用例超时。确定性断言只负责在
真实运行完成后检查输出字段、值、类型或格式，不能代替真实执行。审批节点在无人值守验收时使用用例中的
决策；其他节点不使用 Mock。增量层探测默认最多等待 `OPENAGENT_INCREMENTAL_PROBE_TIMEOUT=120` 秒；
`OPENAGENT_INCREMENTAL_MAX_ITERATIONS` 可设置创建或修改的最大总迭代次数，默认 100，范围 1–10000。
OpenCode 首次规划调用默认最多等待 120 秒（`OPENCODE_GENERATOR_CALL_TIMEOUT`，范围 30–1800）；
修复候选默认最多等待 60 秒（`OPENCODE_REPAIR_CALL_TIMEOUT`，范围 30–600）；只有无文本、reasoning、工具和诊断的静默超时会自动重试一次，第二次仍失败则暂停并保留草稿。
每次 OpenCode 调用还会把用途、进程号、退出码、耗时、超时、诊断摘要和响应尾部写入
`.openagent-logs/opencode.jsonl`；日志已过滤常见 Bearer/API key/token 字段，不记录完整提示词。
可通过 `OPENAGENT_OPENCODE_LOG` 指定其他日志文件路径。日志写入失败不会改变生成任务的真实结果。
Windows 下可在另一个 PowerShell 中运行 `.\scripts\tail-opencode-log.ps1` 实时查看最近 50 条调用记录。
生成器会把可用 Harness Agent 清单交给模型，并要求每个节点同时生成执行参数：Agent
必须包含 `agent_id` 和完整 `prompt`，提示词/循环/输出节点包含 `template`，条件节点包含
`expression` 与分支条件。后端会拒绝不存在的 Agent，并为遗漏的可选参数补充可执行默认值。
右侧标题会显示实际模型和凭证状态。当前项目从项目根目录 `.env` 读取 `OPENAI_API_KEY`，密钥不会
进入浏览器或项目 YAML；读取失败时请求会明确报错，不会切换到其他模型或演示响应。

## 工作流执行

点击右上角“运行”，或在右侧“运行”页输入任务后启动。前端会先保存当前画布，每次执行
都会生成独立的 run 并对已保存配置做快照；后续编辑不会改变正在运行的任务。运行记录当前保存在 Studio
进程内存中，重启后不会保留。

节点按 Dify/n8n 风格分组，所有节点都有持久化参数和执行语义：

左侧节点库支持按中文名称、节点类型、分组和能力说明搜索，分组可以折叠，便于在完整节点集合中快速定位。

- 触发器：`manual_trigger`、`webhook`、`schedule`；Webhook 使用配置的 `/hooks/...`
  路径启动工作流，Schedule 支持带范围、列表和步长的五段 Cron、字段范围校验、IANA 时区和分钟级去重；
- AI：`llm`、`agent`、`tool`、`code` 均通过 Harness v1 task 执行，
  `knowledge_retrieval` 支持查询模板、内置文档和 Top-K 召回；
- 数据处理：`prompt`、`variable_set`、`transform`、`merge`，支持 JSON 解析/序列化、
  路径提取、字段筛选、数组扁平化以及 array/object/concat 合并；
- 集成：`http_request` 支持方法、请求头、模板化请求体、超时和状态码策略；结构化请求体中任意
  嵌套字典或列表里的字符串都支持模板表达式，数字、布尔值和 null 保持原类型。默认禁止
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
执行器会唤醒等待中的审批节点，并通过 SDK 向所有活跃 Harness task 发送取消请求。
环境 setup、Catalog、manifest、沙箱和验证命令全部由独立 Harness 管理；Studio 不调用管理接口，
也不会在任务失败后擅自执行 setup。

Harness 新版沙箱默认拒绝网络。OpenCode 需要访问 DeepSeek API，因此注册脚本在 Harness-owned
三个 OpenCode manifest 都需访问 DeepSeek API，因此 task 网络设为 `allow`。`coding` 只做无工具文本推理；
`repository-analysis` 只允许读取、列出和搜索工作区；`test-runner` 只允许运行已声明的测试命令，工作区保持只读，缓存写入临时目录。
adapter 若只得到“Ready/Task?”等启动提示会返回 `protocol_output_invalid`，不会把未执行任务伪装成成功节点。

## 飞书与 QQ 机器人

`project.yaml` 的 `integrations.feishu[]` 和 `integrations.qq[]` 将平台应用映射到工作流。
凭证字段保存的是环境变量名称，不保存实际密钥。默认示例从项目根目录 `.env` 读取：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFICATION_TOKEN=xxx
FEISHU_ENCRYPT_KEY=xxx
QQ_BOT_APP_ID=1024xxxx
QQ_BOT_SECRET=xxx
```

飞书开放平台的事件订阅请求地址填写：

```text
https://你的域名/integrations/feishu/main/events
```

支持飞书 URL verification、verification token、`X-Lark-Signature` 和加密事件解密；
`im.message.receive_v1` 等事件会转换为标准工作流输入。工作流完成后使用 tenant access token
调用消息回复 API。应用需开通读取与发送消息的权限，并订阅消息事件。

QQ 开放平台的回调地址填写：

```text
https://你的域名/integrations/qq/main/events
```

支持 QQ 官方 `op=13` 回调 URL 验证、Ed25519 请求验签、C2C 私聊、群聊 At 消息、频道 At 消息和
频道私信事件。工作流完成后会根据事件场景调用 `/v2/users`、`/v2/groups` 或 `/v2/channels`
消息接口回复。QQ 机器人需在开放平台配置对应事件订阅和消息权限。

两个平台都采用 10 分钟事件 ID 去重，事件请求体限制为 1 MiB。回调会立即确认并在后台等待工作流，
避免平台超时；自动回复失败不会把已成功接收的事件重新执行。`GET /api/integrations/status`
只返回就绪状态和缺失的环境变量名称，不返回任何凭证值。

本地 `127.0.0.1` 不能直接作为平台回调地址。正式开放需要 HTTPS 公网域名或受信任的反向代理，
并应在代理层增加访问日志、速率限制和请求超时。不要在未启用平台验签的情况下把通用 `/hooks` 暴露公网。

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
- `POST /api/workflows/{workflow_id}/runs`：启动工作流，body 可包含 `input` 和 `relative_path`
- `GET|POST|PUT|PATCH|DELETE /hooks/{path}`：触发 path 匹配的 Webhook 工作流
- `GET /api/workflow-runs?workflow_id=...`：列出运行记录
- `GET /api/workflow-runs/{run_id}`：读取运行和节点状态
- `GET /api/workflow-runs/{run_id}/events`：订阅 SSE 运行事件
- `POST /api/workflow-runs/{run_id}/cancel`：取消运行及活跃 Harness task
- `POST /api/workflow-runs/{run_id}/nodes/{node_id}/approval`：提交人工审批决定
- `GET /api/integrations/status`：读取 QQ/飞书集成就绪状态，不返回凭证
- `POST /integrations/feishu/{integration_id}/events`：飞书官方事件订阅回调
- `POST /integrations/qq/{integration_id}/events`：QQ 官方机器人回调

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
    backend_id: default
    agent_id: builder
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
