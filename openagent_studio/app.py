from __future__ import annotations

import os
import signal
import socket
import subprocess
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from agent_harness_sdk import HarnessAPIError
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from .compiler import compile_opencode
from .harness_client import DEFAULT_BACKEND_ID, create_harness_client
from .models import ProjectSpec, WorkflowSpec
from .store import SpecStore
from .generator import GeneratorManager
from .workflow_runner import TERMINAL_RUN_STATES, WorkflowManager, validate_executable_workflow
from .platform_integrations import PlatformIntegrationManager


def create_app(spec_path: Path | None = None) -> FastAPI:
    store = SpecStore(spec_path or Path(os.environ.get("OPENAGENT_SPEC", "project.yaml")))
    generator = GeneratorManager(store)
    workflows = WorkflowManager(os.environ.get("AGENT_HARNESS_URL", "http://127.0.0.1:8765"))
    platforms = PlatformIntegrationManager(workflows)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        workflows.start_scheduler(store.load)
        yield
        workflows.stop_scheduler()

    app = FastAPI(title="OpenAgent Studio", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.generator_manager = generator
    app.state.workflow_manager = workflows
    app.state.platform_integration_manager = platforms
    static_dir = Path(__file__).parent / "static"
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

    @app.get("/api/spec")
    def get_spec():
        return {"etag": store.etag(), "spec": store.load().model_dump(mode="json", exclude_none=True)}

    @app.put("/api/spec")
    def put_spec(spec: ProjectSpec, if_match: str | None = Header(default=None)):
        try:
            etag = store.save(spec, if_match)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"etag": etag, "spec": spec.model_dump(mode="json", exclude_none=True)}

    @app.post("/api/spec/validate")
    def validate_spec(spec: ProjectSpec):
        return {"valid": True, "message": "配置校验通过"}

    @app.get("/api/spec/summary")
    def spec_summary():
        spec = store.load()
        return {
            "name": spec.name,
            "agent_count": len(spec.agents),
            "provider_count": len(spec.providers),
            "workflow_count": len(spec.workflows),
        }

    @app.get("/api/form-options")
    def form_options():
        return {
            "agent_modes": [{"value": "primary", "label": "主智能体"}, {"value": "subagent", "label": "子智能体"}, {"value": "all", "label": "两种都可以"}],
            "permissions": [{"value": "allow", "label": "自动允许"}, {"value": "ask", "label": "每次询问"}, {"value": "deny", "label": "禁止"}],
            "runtime_types": [{"value": "service", "label": "后台服务"}, {"value": "task", "label": "一次性任务"}],
        }

    @app.get("/api/workflows")
    def list_workflows():
        return [workflow.model_dump(mode="json") for workflow in store.load().workflows]

    @app.get("/api/workflows/{workflow_id}")
    def get_workflow(workflow_id: str):
        workflow = next((item for item in store.load().workflows if item.id == workflow_id), None)
        if workflow is None:
            raise HTTPException(status_code=404, detail="找不到工作流")
        return workflow.model_dump(mode="json")

    @app.post("/api/workflows/{workflow_id}/validate")
    def validate_workflow(workflow_id: str):
        workflow = next((item for item in store.load().workflows if item.id == workflow_id), None)
        if workflow is None:
            raise HTTPException(status_code=404, detail="找不到工作流")
        errors = validate_executable_workflow(store.load(), workflow)
        return {"valid": not errors, "errors": errors}

    @app.put("/api/workflows/{workflow_id}")
    def put_workflow(workflow_id: str, workflow: WorkflowSpec, if_match: str | None = Header(default=None)):
        if workflow.id != workflow_id:
            raise HTTPException(status_code=422, detail="工作流编号不一致")
        current = store.load()
        if not any(item.id == workflow_id for item in current.workflows):
            raise HTTPException(status_code=404, detail="找不到工作流")
        node_ids = {node.id for node in workflow.nodes}
        if any(edge.source not in node_ids or edge.target not in node_ids for edge in workflow.edges):
            raise HTTPException(status_code=422, detail="连线引用了不存在的节点")
        current.workflows = [workflow if item.id == workflow_id else item for item in current.workflows]
        try:
            etag = store.save(current, if_match)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"etag": etag, "workflow": workflow.model_dump(mode="json")}

    @app.post("/api/workflows/{workflow_id}/runs", status_code=202)
    def start_workflow_run(workflow_id: str, body: dict[str, object] | None = None):
        try:
            run = workflows.start(store.load(), workflow_id, body)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到工作流") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return run.payload()

    @app.api_route("/hooks/{hook_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], status_code=202)
    async def trigger_workflow_webhook(hook_path: str, request: Request):
        project = store.load()
        requested_path = "/hooks/" + hook_path.strip("/")
        matches = [(workflow, node) for workflow in project.workflows for node in workflow.nodes if node.type == "webhook" and str(node.data.get("path", "")).rstrip("/") == requested_path.rstrip("/")]
        if not matches:
            raise HTTPException(status_code=404, detail="找不到 Webhook 节点")
        if len(matches) > 1:
            raise HTTPException(status_code=409, detail="Webhook 路径重复")
        workflow, node = matches[0]
        expected = str(node.data.get("method", "POST")).upper()
        if request.method != expected:
            raise HTTPException(status_code=405, detail=f"Webhook 只接受 {expected}")
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = raw.decode(errors="replace")
        run = workflows.start(project, workflow.id, {"input": {"trigger": "webhook", "node_id": node.id, "query": dict(request.query_params), "headers": dict(request.headers), "body": body}, "_trigger_node_id": node.id})
        return run.payload()

    @app.get("/api/integrations/status")
    def integration_status():
        return platforms.status(store.load())

    @app.post("/integrations/feishu/{integration_id}/events")
    async def feishu_events(integration_id: str, request: Request):
        raw = await _limited_body(request)
        try:
            config = platforms.require_feishu(store.load(), integration_id)
            return platforms.handle_feishu(store.load(), config, raw, {key.lower(): value for key, value in request.headers.items()})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到飞书集成") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="飞书事件不是有效 JSON") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/integrations/qq/{integration_id}/events")
    async def qq_events(integration_id: str, request: Request):
        raw = await _limited_body(request)
        try:
            project = store.load()
            config = platforms.require_qq(project, integration_id)
            return platforms.handle_qq(project, config, raw, {key.lower(): value for key, value in request.headers.items()})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到 QQ 集成") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="QQ 事件不是有效 JSON") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/workflow-runs")
    def list_workflow_runs(workflow_id: str | None = None):
        values = list(workflows.runs.values())
        if workflow_id:
            values = [run for run in values if run.workflow_id == workflow_id]
        return [run.payload() for run in sorted(values, key=lambda item: item.created_at, reverse=True)]

    @app.get("/api/workflow-runs/{run_id}")
    def get_workflow_run(run_id: str):
        try:
            return workflows.require(run_id).payload()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到工作流运行记录") from exc

    @app.get("/api/workflow-runs/{run_id}/events")
    def workflow_run_events(run_id: str):
        try:
            run = workflows.require(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到工作流运行记录") from exc

        def stream():
            cursor = 0
            while True:
                with run.event_signal:
                    if cursor >= len(run.events) and run.status not in TERMINAL_RUN_STATES:
                        run.event_signal.wait(timeout=15)
                    events = run.events[cursor:]
                    cursor = len(run.events)
                for item in events:
                    yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                if run.status in TERMINAL_RUN_STATES and cursor >= len(run.events):
                    return
                if not events:
                    yield ": heartbeat\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/workflow-runs/{run_id}/cancel")
    def cancel_workflow_run(run_id: str):
        try:
            return workflows.cancel(run_id).payload()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到工作流运行记录") from exc

    @app.post("/api/workflow-runs/{run_id}/nodes/{node_id}/approval")
    def resolve_workflow_approval(run_id: str, node_id: str, body: dict[str, object]):
        try:
            run = workflows.approve(run_id, node_id, bool(body.get("approved")), str(body.get("comment", "")))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到工作流运行记录") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return run.payload()

    @app.get("/api/generator/workflows/{workflow_id}/messages")
    def generator_messages(workflow_id: str):
        return generator.history.get(workflow_id, [])

    @app.get("/api/generator/status")
    def generator_status():
        try:
            return generator.status()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/generator/workflows/{workflow_id}/messages", status_code=202)
    def generator_message(workflow_id: str, body: dict[str, str]):
        try:
            generation = generator.start(workflow_id, body.get("message", ""))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到工作流") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"generation_id": generation.id, "workflow_id": workflow_id}

    @app.post("/api/generator/workflows/{workflow_id}/optimize", status_code=202)
    def optimize_workflow(workflow_id: str):
        try:
            generation = generator.optimize(workflow_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到工作流") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"generation_id": generation.id, "workflow_id": workflow_id}

    @app.get("/api/generator/generations/{generation_id}/events")
    def generator_events(generation_id: str):
        try:
            generation = generator.require(generation_id)
        except KeyError:
            # Generations are intentionally process-local. After Studio restarts,
            # an already-open browser tab may reconnect its old EventSource.
            # Return the existing terminal failure event so old and new browser
            # bundles close instead of retrying a permanent 404 forever.
            data = json.dumps({
                "generation_id": generation_id,
                "reason": "expired",
                "message": "生成任务已失效（Studio 可能已重启），请重新发起优化",
            }, ensure_ascii=False)

            def expired_stream():
                yield f"event: generation.failed\ndata: {data}\n\n"

            return StreamingResponse(
                expired_stream(), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        def stream():
            cursor = 0
            while True:
                while cursor < len(generation.events):
                    item = generation.events[cursor]
                    cursor += 1
                    yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                if generation.completed and cursor >= len(generation.events):
                    return
                try:
                    generation.event_queue.get(timeout=15)
                except Exception:
                    yield ": heartbeat\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/generator/generations/{generation_id}/cancel")
    def generator_cancel(generation_id: str):
        try:
            generation = generator.cancel(generation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到生成任务") from exc
        return {"cancelled": True, "generation_id": generation.id}

    @app.get("/api/compile/opencode")
    def get_opencode():
        return compile_opencode(store.load())

    @app.get("/api/runtime/agents")
    def runtime_agents():
        return [item.model_dump(mode="json") for item in store.load().harness]

    @app.get("/api/runtime/status")
    def runtime_status():
        spec = store.load()
        backend_ids = sorted({item.backend_id for item in spec.harness} or {DEFAULT_BACKEND_ID})
        backends: dict[str, dict[str, object]] = {}
        for backend_id in backend_ids:
            client = None
            try:
                client = create_harness_client(backend_id, timeout=2)
                capabilities = client.capabilities()
                task_agents = None
                task_agent_error = ""
                try:
                    task_agents = client.task_agents()
                except (HarnessAPIError, httpx.HTTPError, AttributeError) as exc:
                    task_agent_error = str(exc)
                expected = {item.agent_id: item for item in spec.harness if item.backend_id == backend_id and item.agent_id}
                identity_mismatch: list[str] = []
                readiness: dict[str, object] = {}
                for agent_id, requirement in expected.items():
                    descriptor = next((item for item in (task_agents or []) if str(item.get("id")) == agent_id), None)
                    if descriptor is None:
                        identity_mismatch.append(f"缺少任务 Agent {agent_id}")
                        continue
                    labels = descriptor.get("labels") or {}
                    required_labels = requirement.labels or {}
                    if any(labels.get(key) != value for key, value in required_labels.items()):
                        identity_mismatch.append(f"任务 Agent {agent_id} 身份标签不匹配")
                    item_readiness = descriptor.get("readiness") or {}
                    readiness[agent_id] = {
                        "state": item_readiness.get("state"),
                        "error_code": item_readiness.get("error_code"),
                        "accepts_tasks": descriptor.get("accepts_tasks", False),
                    }
                actionable = ""
                if task_agent_error:
                    actionable = "需要检查 Harness API v1 和任务 Token"
                elif identity_mismatch:
                    actionable = "需要使用管理权限注册正确的 coding Agent"
                elif any(item.get("state") != "ready" or not item.get("accepts_tasks") for item in readiness.values()):
                    actionable = "需要完成 Harness Agent setup"
                backends[backend_id] = {
                    "running": True,
                    "api_version": capabilities.get("api", {}).get("selected_version"),
                    "capabilities": capabilities,
                    "task_agents": task_agents,
                    "task_agent_error": task_agent_error,
                    "identity_mismatch": identity_mismatch,
                    "readiness": readiness,
                    "actionable_error": actionable,
                    "error": "",
                }
            except (HarnessAPIError, httpx.HTTPError, RuntimeError) as exc:
                backends[backend_id] = {
                    "running": False,
                    "api_version": None,
                    "capabilities": None,
                    "task_agents": None,
                    "task_agent_error": "",
                    "identity_mismatch": [],
                    "readiness": {},
                    "actionable_error": "需要启动正确的 Harness 实例",
                    "error": str(exc),
                }
            finally:
                if client is not None:
                    client.close()
        return {"running": all(item["running"] for item in backends.values()), "managed": False, "backends": backends}

    @app.get("/")
    def index():
        index_file = static_dir / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return HTMLResponse("<h1>前端尚未构建</h1><p>请在 frontend 目录运行 npm install 和 npm run build。</p>", status_code=503)

    return app


async def _limited_body(request: Request, limit: int = 1024 * 1024) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > limit:
        raise HTTPException(status_code=413, detail="平台事件请求体不能超过 1 MiB")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="平台事件请求体不能超过 1 MiB")
        chunks.append(chunk)
    return b"".join(chunks)


app = create_app()


def _windows_listener_pids(output: str, port: int) -> set[int]:
    pids: set[int] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP" or fields[-2].upper() != "LISTENING":
            continue
        try:
            local_port = int(fields[1].rsplit(":", 1)[1])
            pid = int(fields[-1])
        except (IndexError, ValueError):
            continue
        if local_port == port:
            pids.add(pid)
    return pids


def _listener_pids(port: int) -> set[int]:
    if os.name == "nt":
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"无法检查端口 {port}：{result.stderr.strip() or 'netstat 执行失败'}")
        return _windows_listener_pids(result.stdout, port)

    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return set()
    if result.returncode not in (0, 1):
        raise RuntimeError(f"无法检查端口 {port}：{result.stderr.strip() or 'lsof 执行失败'}")
    return {int(line) for line in result.stdout.splitlines() if line.strip().isdigit()}


def _port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _stop_port_listeners(host: str, port: int) -> set[int]:
    if os.environ.get("OPENAGENT_KILL_PORT", "1") == "0":
        return set()

    pids = _listener_pids(port) - {os.getpid()}
    for pid in sorted(pids):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    if not pids:
        return pids

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _port_is_available(host, port):
            print(f"已结束占用 {host}:{port} 的旧进程：{', '.join(map(str, sorted(pids)))}")
            return pids
        time.sleep(0.05)
    raise RuntimeError(f"结束进程后端口 {host}:{port} 仍未释放")


def main() -> None:
    import uvicorn

    host = "127.0.0.1"
    port = 8787
    _stop_port_listeners(host, port)
    global app
    app = create_app()
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
