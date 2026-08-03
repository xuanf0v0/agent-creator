from __future__ import annotations

import os
import signal
import subprocess
import json
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from .compiler import compile_harness, compile_opencode
from .models import ProjectSpec, WorkflowSpec
from .store import SpecStore
from .generator import GeneratorManager


class HarnessManager:
    def __init__(self) -> None:
        root = Path(os.environ.get("AGENT_HARNESS_ROOT", Path.home() / "agent-harness"))
        self.command = Path(os.environ.get("AGENT_HARNESS_BIN", root / ".venv/bin/agent-harness"))
        self.manifests = Path(os.environ.get("AGENT_HARNESS_MANIFESTS", root / "agents"))
        self.root = root
        self.process: subprocess.Popen | None = None
        self.error = ""
        self.operations: dict[str, dict[str, str]] = {}

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
    manager = HarnessManager()
    generator = GeneratorManager(store)
    enabled = auto_start_harness if auto_start_harness is not None else os.environ.get("OPENAGENT_START_HARNESS", "1") != "0"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if enabled:
            manager.start()
        yield
        manager.stop()

    app = FastAPI(title="OpenAgent Studio", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.harness_manager = manager
    app.state.generator_manager = generator
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
        node_ids = {node.id for node in workflow.nodes}
        errors = []
        for edge in workflow.edges:
            if edge.source not in node_ids or edge.target not in node_ids:
                errors.append(f"连线引用了不存在的节点：{edge.source} → {edge.target}")
            if edge.source == edge.target:
                errors.append(f"节点不能连接到自身：{edge.source}")
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

    @app.get("/api/generator/workflows/{workflow_id}/messages")
    def generator_messages(workflow_id: str):
        return generator.history.get(workflow_id, [])

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


def main() -> None:
    import uvicorn
    uvicorn.run("openagent_studio.app:app", host="127.0.0.1", port=8787, reload=False)


if __name__ == "__main__":
    main()
