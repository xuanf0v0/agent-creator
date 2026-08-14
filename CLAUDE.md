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
