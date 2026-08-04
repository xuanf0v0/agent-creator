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
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from .compiler import compile_harness, compile_opencode
from .models import ProjectSpec, WorkflowSpec
from .store import SpecStore
from .generator import GeneratorManager
from .workflow_runner import TERMINAL_RUN_STATES, WorkflowManager, validate_executable_workflow
from .platform_integrations import PlatformIntegrationManager


class HarnessManager:
    def __init__(self, project_root: Path | None = None) -> None:
        root = Path(os.environ.get("AGENT_HARNESS_ROOT", Path.home() / "agent-harness"))
        self.command = Path(os.environ.get("AGENT_HARNESS_BIN", root / ".venv/bin/agent-harness"))
        self.manifests = Path(os.environ.get("AGENT_HARNESS_MANIFESTS", (project_root or Path.cwd()) / ".openagent-agents"))
        self.root = root
        self.process: subprocess.Popen | None = None
        self.error = ""
        self.operations: dict[str, dict[str, str]] = {}

    def sync(self, spec: ProjectSpec) -> None:
        if not spec.harness or os.environ.get("OPENAGENT_SYNC_HARNESS", "1") == "0":
            return
        try:
            self.manifests.mkdir(parents=True, exist_ok=True)
            for item in spec.harness:
                target = self.manifests / f"{item.id}.yaml"
                content = compile_harness(spec, item.id)
                if not target.is_file() or target.read_text(encoding="utf-8") != content:
                    temporary = target.with_suffix(".yaml.tmp")
                    temporary.write_text(content, encoding="utf-8")
                    temporary.replace(target)
        except OSError as exc:
            self.error = f"无法同步 Harness manifests：{exc}"
            raise RuntimeError(self.error) from exc

    def reachable(self) -> bool:
        try:
            return httpx.get(os.environ.get("AGENT_HARNESS_URL", "http://127.0.0.1:8765") + "/health", timeout=0.5).status_code == 200
        except httpx.HTTPError:
            return False

    def start(self) -> None:
        if self.reachable():
            return
        if not self.command.is_file():
            self.error = f"找不到 Harness 启动程序：{self.command}"
            return
        try:
            self.process = subprocess.Popen(
                [str(self.command), "serve", "--manifests", str(self.manifests)],
                cwd=self.root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.reachable():
                    self.error = ""
                    return
                if self.process.poll() is not None:
                    self.error = f"Harness 启动进程已退出（code={self.process.returncode}）"
                    return
                time.sleep(0.1)
            self.error = "Harness 启动超时，请检查 manifest 和 Harness 日志"
        except OSError as exc:
            self.error = f"Harness 启动失败：{exc}"

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            self.process.kill()


def create_app(spec_path: Path | None = None, auto_start_harness: bool | None = None) -> FastAPI:
    store = SpecStore(spec_path or Path(os.environ.get("OPENAGENT_SPEC", "project.yaml")))
    manager = HarnessManager(store.path.resolve().parent)
    generator = GeneratorManager(store)
    workflows = WorkflowManager(os.environ.get("AGENT_HARNESS_URL", "http://127.0.0.1:8765"))
    platforms = PlatformIntegrationManager(workflows)
    enabled = auto_start_harness if auto_start_harness is not None else os.environ.get("OPENAGENT_START_HARNESS", "1") != "0"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if enabled:
            try:
                manager.sync(store.load())
                manager.start()
            except RuntimeError:
                pass
        workflows.start_scheduler(store.load)
        yield
        workflows.stop_scheduler()
        manager.stop()

    app = FastAPI(title="OpenAgent Studio", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.harness_manager = manager
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
        if enabled:
            try:
                manager.sync(spec)
            except RuntimeError:
                pass
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

    @app.get("/api/generator/generations/{generation_id}/events")
    def generator_events(generation_id: str):
        try:
            generation = generator.require(generation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="找不到生成任务") from exc

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

    @app.get("/api/compile/harness/{agent_id}")
    def get_harness(agent_id: str):
        try:
            return HTMLResponse(compile_harness(store.load(), agent_id), media_type="text/yaml")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown harness agent: {agent_id}") from exc

    @app.get("/api/runtime/agents")
    def runtime_agents():
        return _harness_request("GET", "/api/agents")

    @app.get("/api/runtime/status")
    def runtime_status():
        return {"running": manager.reachable(), "managed": manager.process is not None, "error": manager.error, "operations": manager.operations}

    @app.post("/api/runtime/agents/{agent_id}/{operation}", status_code=202)
    def runtime_operation(agent_id: str, operation: str, background_tasks: BackgroundTasks):
        if operation not in {"setup", "start", "stop", "restart"}:
            raise HTTPException(status_code=404, detail="unsupported operation")
        key = f"{agent_id}:{operation}"
        if manager.operations.get(key, {}).get("state") == "running":
            return {"accepted": True, "message": "操作正在执行"}
        manager.operations[key] = {"state": "running", "message": "操作正在执行"}
        background_tasks.add_task(_run_harness_operation, manager, key, agent_id, operation)
        return {"accepted": True, "message": "操作已提交"}

    @app.get("/api/runtime/agents/{agent_id}/logs")
    def runtime_logs(agent_id: str, lines: int = 100):
        return _harness_request("GET", f"/api/agents/{agent_id}/logs?lines={lines}")

    @app.post("/api/runtime/agents/{agent_id}/echo/test")
    def runtime_echo(agent_id: str, body: dict[str, str]):
        message = body.get("message", "").strip()
        if not message:
            raise HTTPException(status_code=422, detail="请输入测试消息")
        return _harness_request("POST", f"/api/agents/{agent_id}/service/echo", {"message": message})

    @app.get("/")
    def index():
        index_file = static_dir / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return HTMLResponse("<h1>前端尚未构建</h1><p>请在 frontend 目录运行 npm install 和 npm run build。</p>", status_code=503)

    return app


def _harness_request(method: str, path: str, json_body: dict | None = None):
    base = os.environ.get("AGENT_HARNESS_URL", "http://127.0.0.1:8765").rstrip("/")
    try:
        with httpx.Client(timeout=15) as client:
            response = client.request(method, base + path, json=json_body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"harness unavailable: {exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail)
    try:
        return response.json()
    except ValueError:
        return {"body": response.text}


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


def _run_harness_operation(manager: HarnessManager, key: str, agent_id: str, operation: str) -> None:
    try:
        base = os.environ.get("AGENT_HARNESS_URL", "http://127.0.0.1:8765").rstrip("/")
        timeout = 1800 if operation == "setup" else 180
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{base}/api/agents/{agent_id}/{operation}")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            manager.operations[key] = {"state": "error", "message": str(detail)}
        else:
            manager.operations[key] = {"state": "completed", "message": "操作已完成"}
    except httpx.HTTPError as exc:
        manager.operations[key] = {"state": "error", "message": f"运行服务连接失败：{exc}"}


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
    uvicorn.run("openagent_studio.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
