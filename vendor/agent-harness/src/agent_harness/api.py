from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import os
import json
import asyncio
import secrets
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .catalog import AgentCatalog, configured_allowed_roots
from .config_store import ConfigStore
from .models import AgentState
from .environment import fingerprint_details
from .instructions import load_instructions, safe_task_directory
from .state_store import StateStore
from .state_store import StateConflict
from .supervisor import AgentSupervisor
from .task_runner import TaskRunner
from .reliability import ReliabilityManager
from .instance_lock import InstanceLock

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}


class TaskCreateRequest(BaseModel):
    agent_id: str
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=100_000)
    relative_path: str = "."


def create_app(manifest_dir: Path, state_path: Path | None = None, lock_address: str = "", allowed_roots: list[Path] | None = None) -> FastAPI:
    catalog = AgentCatalog.load(manifest_dir, allowed_roots or configured_allowed_roots())
    supervisor = AgentSupervisor(catalog)
    config = ConfigStore()
    home = Path(os.environ.get("AGENT_HARNESS_HOME", Path.home() / ".agent-harness"))
    effective_home = state_path.parent if state_path else home
    instance_lock = InstanceLock(effective_home / "harness.lock", lock_address)
    instance_lock.acquire()
    try:
        store = StateStore(state_path or home / "state.db")
    except Exception as exc:
        return _degraded_app(instance_lock, exc)
    runner = TaskRunner(catalog, store, supervisor)
    reliability = ReliabilityManager(effective_home, store, runner, catalog, instance_lock)
    reload_lock = asyncio.Lock()
    reload_task: asyncio.Task[None] | None = None

    def manifest_signature() -> tuple[tuple[str, int, int], ...]:
        paths = sorted([*manifest_dir.glob("*.yaml"), *manifest_dir.glob("*.yml")])
        return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)

    async def apply_reload() -> dict[str, Any]:
        async with reload_lock:
            if runner._active or runner._active_calls:
                raise RuntimeError("cannot reload while tasks are active")
            previous = dict(catalog._agents)
            try:
                changes = catalog.reload()
                supervisor.reload_catalog()
                await runner.reload_catalog()
            except Exception:
                catalog._agents = previous
                raise
            runner.ready_error = ""
            store.add_system_event("manifest.reloaded", changes)
            return {"reloaded": True, "changes": changes, "agents": len(catalog.all())}

    async def watch_manifests() -> None:
        signature = manifest_signature()
        while True:
            await asyncio.sleep(1)
            current = manifest_signature()
            if current == signature: continue
            try:
                await apply_reload()
                signature = current
            except Exception as exc:
                runner.ready_error = f"manifest reload failed: {exc}"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal reload_task
        await reliability.start()
        await runner.start()
        reload_task = asyncio.create_task(watch_manifests())
        try:
            yield
        finally:
            if reload_task:
                reload_task.cancel()
                await asyncio.gather(reload_task, return_exceptions=True)
            reliability.shutting_down = True
            await runner.shutdown(grace_seconds=10)
            await supervisor.shutdown()
            await reliability.shutdown()

    app = FastAPI(title="Agent Harness", version="0.1.0", lifespan=lifespan)
    app.state.catalog = catalog
    app.state.supervisor = supervisor
    app.state.config_store = config
    app.state.state_store = store
    app.state.task_runner = runner
    app.state.reliability = reliability
    setup_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}

    @app.middleware("http")
    async def authenticate_and_audit(request: Request, call_next):
        token = os.environ.get("AGENT_HARNESS_TOKEN", "")
        if token and request.url.path not in {"/health", "/ready"}:
            supplied = request.headers.get("authorization", "")
            if not supplied.startswith("Bearer ") or not secrets.compare_digest(supplied[7:], token):
                store.add_audit(request.headers.get("x-actor", "anonymous"), request.method, request.url.path, 401)
                return Response(content=json.dumps({"detail": {"code": "unauthorized", "message": "valid bearer token required"}}), status_code=401, media_type="application/json")
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            store.add_audit(request.headers.get("x-actor", "local"), request.method, request.url.path, response.status_code)
        return response

    def require_ready() -> None:
        try: reliability.require_ready()
        except RuntimeError as exc: raise HTTPException(status_code=503, detail=str(exc)) from exc

    def require(agent_id: str):
        try:
            return catalog.require(agent_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}") from exc

    def status_payload(agent_id: str) -> dict[str, Any]:
        agent = require(agent_id)
        runtime = supervisor.status(agent_id).model_dump(mode="json")
        current, _, _ = fingerprint_details(agent)
        prepared = store.environment(agent_id)
        latest = store.latest_setup(agent_id)
        drifted = prepared is None or prepared["fingerprint"] != current
        if runtime["status"] == "setting_up" or latest and latest["status"] in {"queued", "preparing"}:
            lifecycle_state = "preparing"
        elif runtime["status"] == "error" or latest and latest["status"] == "error":
            lifecycle_state = "error"
        elif drifted:
            lifecycle_state = "setup_required"
        else:
            lifecycle_state = "ready"
        return {**runtime, "lifecycle_state": lifecycle_state, "setup_required": drifted, "fingerprint": current, "latest_setup": latest}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        status = reliability.status()
        if not status["ready"]:
            raise HTTPException(status_code=503, detail=status)
        return status

    @app.get("/api/system/status")
    async def system_status() -> dict[str, Any]:
        return reliability.status()

    @app.get("/api/system/audit")
    async def audit_log(limit: int = 200) -> list[dict[str, Any]]:
        if not 1 <= limit <= 2000:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit", "message": "limit must be between 1 and 2000"})
        return store.audit_entries(limit)

    @app.get("/metrics")
    async def metrics() -> Response:
        task_counts = {row["status"]: row["count"] for row in store._db.execute("SELECT status,COUNT(*) AS count FROM tasks GROUP BY status")}
        setup_counts = {row["status"]: row["count"] for row in store._db.execute("SELECT status,COUNT(*) AS count FROM setup_operations GROUP BY status")}
        lines = ["# TYPE agent_harness_tasks gauge"]
        lines.extend(f'agent_harness_tasks{{status="{status}"}} {count}' for status, count in sorted(task_counts.items()))
        lines.append("# TYPE agent_harness_setup_operations gauge")
        lines.extend(f'agent_harness_setup_operations{{status="{status}"}} {count}' for status, count in sorted(setup_counts.items()))
        lines.append(f"agent_harness_uptime_seconds {time.time() - reliability.started_at}")
        return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.post("/api/system/maintain")
    async def system_maintain() -> dict[str, Any]:
        require_ready()
        return store.maintain()

    @app.post("/api/system/reload")
    async def reload_manifests() -> dict[str, Any]:
        require_ready()
        try:
            return await apply_reload()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail={"code": "runtime_busy", "message": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "manifest_invalid", "message": str(exc)}) from exc

    @app.get("/api/agents")
    async def list_agents() -> list[dict[str, Any]]:
        return [status_payload(item.id) for item in catalog.all()]

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str) -> dict[str, Any]:
        require(agent_id)
        return status_payload(agent_id)

    async def lifecycle(agent_id: str, operation: str) -> dict[str, Any]:
        require(agent_id)
        if operation not in {"stop"}:
            require_ready()
        try:
            result = await getattr(supervisor, operation)(agent_id)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return result.model_dump(mode="json")

    async def perform_setup(agent_id: str, operation_id: str) -> dict[str, Any]:
        store.update_setup(operation_id, "preparing")
        try:
            payload = await lifecycle(agent_id, "setup")
            if payload["status"] == "stopped":
                value, files, hashes = fingerprint_details(require(agent_id))
                store.set_environment(agent_id, value, files, hashes)
                store.update_setup(operation_id, "ready", fingerprint=value, logs=supervisor.logs.tail(agent_id, 2000))
            else:
                store.update_setup(operation_id, "error", error_code="setup_failed", error_message=payload.get("error_message", "setup failed"), logs=supervisor.logs.tail(agent_id, 2000))
            return {**payload, "setup_operation": store.setup_operation(operation_id)}
        except Exception as exc:
            store.update_setup(operation_id, "error", error_code="setup_failed", error_message=str(exc), logs=supervisor.logs.tail(agent_id, 2000))
            raise
        finally:
            setup_tasks.pop(agent_id, None)

    @app.post("/api/agents/{agent_id}/setup")
    async def setup_agent(agent_id: str, request: Request, response: Response) -> dict[str, Any]:
        require(agent_id); require_ready()
        operation = store.start_setup(agent_id, request.headers.get("Idempotency-Key"))
        created = bool(operation.pop("_created", False))
        active = setup_tasks.get(agent_id)
        if not created and operation["status"] in {"ready", "error"}:
            return {"accepted": False, "replayed": True, "setup_operation": operation}
        if created and (active is None or active.done()):
            active = asyncio.create_task(perform_setup(agent_id, operation["id"]))
            setup_tasks[agent_id] = active
        if "respond-async" in request.headers.get("Prefer", ""):
            response.status_code = 202
            return {"accepted": True, "setup_operation": operation}
        if active is None:
            return {"accepted": False, "setup_operation": store.setup_operation(operation["id"])}
        return await active

    @app.get("/api/agents/{agent_id}/setup")
    async def setup_status(agent_id: str) -> dict[str, Any]:
        require(agent_id)
        operation = store.latest_setup(agent_id)
        return {"agent_id": agent_id, "setup_operation": operation, "logs": supervisor.logs.tail(agent_id, 200)}

    @app.get("/api/setup-operations/{operation_id}")
    async def setup_operation(operation_id: str) -> dict[str, Any]:
        try: return store.setup_operation(operation_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail={"code": "setup_operation_not_found", "message": "unknown setup operation"}) from exc

    @app.post("/api/agents/{agent_id}/start")
    async def start_agent(agent_id: str) -> dict[str, Any]:
        return await lifecycle(agent_id, "start")

    @app.post("/api/agents/{agent_id}/stop")
    async def stop_agent(agent_id: str) -> dict[str, Any]:
        return await lifecycle(agent_id, "stop")

    @app.post("/api/agents/{agent_id}/restart")
    async def restart_agent(agent_id: str) -> dict[str, Any]:
        return await lifecycle(agent_id, "restart")

    @app.get("/api/agents/{agent_id}/logs")
    async def get_logs(agent_id: str, lines: int = 200) -> dict[str, Any]:
        require(agent_id)
        if not 0 <= lines <= 2000:
            raise HTTPException(status_code=422, detail="lines must be between 0 and 2000")
        values = supervisor.logs.tail(agent_id, lines)
        return {"agent_id": agent_id, "lines": values, "total": len(values)}

    @app.websocket("/ws/agents/{agent_id}/logs")
    async def log_socket(websocket: WebSocket, agent_id: str) -> None:
        token = os.environ.get("AGENT_HARNESS_TOKEN", "")
        supplied = websocket.headers.get("authorization", "")
        query_token = websocket.query_params.get("token", "")
        if token and not ((supplied.startswith("Bearer ") and secrets.compare_digest(supplied[7:], token)) or secrets.compare_digest(query_token, token)):
            await websocket.close(code=4401)
            return
        if catalog.get(agent_id) is None:
            await websocket.close(code=4404)
            return
        queue = supervisor.logs.subscribe(agent_id)
        try:
            await websocket.accept()
            for line in supervisor.logs.tail(agent_id, 100):
                await websocket.send_text(line)
            while True:
                await websocket.send_text(await queue.get())
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            supervisor.logs.unsubscribe(agent_id, queue)

    @app.get("/api/agents/{agent_id}/config")
    async def get_config(agent_id: str) -> list[dict[str, Any]]:
        return config.get(require(agent_id))

    @app.put("/api/agents/{agent_id}/config")
    async def put_config(agent_id: str, body: dict[str, str]) -> list[dict[str, Any]]:
        require_ready()
        try:
            return config.update(require(agent_id), body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/agents/{agent_id}/instructions")
    async def instructions(agent_id: str, relative_path: str = ".") -> dict[str, Any]:
        agent = require(agent_id)
        try:
            directory = safe_task_directory(agent.cwd, relative_path)
            bundle = load_instructions(agent.cwd, directory)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"content": bundle.content, "sha256": bundle.sha256, "sources": bundle.sources}

    @app.get("/api/agents/{agent_id}/environment")
    async def environment_status(agent_id: str) -> dict[str, Any]:
        agent = require(agent_id)
        current, files, hashes = fingerprint_details(agent)
        prepared = store.environment(agent_id)
        previous_hashes = prepared.get("file_hashes", {}) if prepared else {}
        changed_files = sorted(path for path in set(previous_hashes) | set(hashes) if previous_hashes.get(path) != hashes.get(path))
        drifted = prepared is None or prepared["fingerprint"] != current
        return {
            "current_fingerprint": current, "files": files, "prepared": prepared,
            "previous_fingerprint": prepared["fingerprint"] if prepared else None,
            "changed_files": changed_files, "drifted": drifted,
            "state": "setup_required" if drifted else "ready",
            "setup_required": drifted, "latest_setup": store.latest_setup(agent_id),
        }

    @app.post("/api/tasks", status_code=202)
    async def create_task(body: TaskCreateRequest, request: Request) -> dict[str, Any]:
        require(body.agent_id)
        require_ready()
        try:
            return await runner.create(body.agent_id, body.title, body.prompt, body.relative_path, request.headers.get("Idempotency-Key"))
        except OverflowError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except StateConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/tasks")
    async def list_tasks(agent_id: str | None = None) -> list[dict[str, Any]]:
        if agent_id: require(agent_id)
        return store.tasks(agent_id)

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        try: return store.task(task_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="Unknown task") from exc

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: str) -> list[dict[str, Any]]:
        try: store.task(task_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="Unknown task") from exc
        return store.events(task_id)

    @app.get("/api/events/stream")
    async def event_stream(request: Request):
        async def stream():
            cursor = int(request.headers.get("last-event-id", "0") or 0)
            while True:
                if await request.is_disconnected(): return
                events = store.system_events(cursor, 100)
                for item in events:
                    cursor = item["id"]
                    yield f"id: {cursor}\nevent: {item['kind']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                if not events: yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/tasks/{task_id}/logs")
    async def task_logs(task_id: str, lines: int = 500) -> list[dict[str, Any]]:
        try: store.task(task_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="Unknown task") from exc
        if not 0 <= lines <= 5000: raise HTTPException(status_code=422, detail="lines must be between 0 and 5000")
        return store.logs(task_id, lines)

    @app.post("/api/tasks/{task_id}/retry", status_code=202)
    async def retry_task(task_id: str, request: Request) -> dict[str, Any]:
        require_ready()
        try: return await runner.retry(task_id, request.headers.get("Idempotency-Key"))
        except KeyError as exc: raise HTTPException(status_code=404, detail="Unknown task") from exc
        except (ValueError, StateConflict) as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OverflowError as exc: raise HTTPException(status_code=429, detail=str(exc)) from exc

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        try: return await runner.cancel(task_id)
        except KeyError as exc: raise HTTPException(status_code=404, detail="Unknown task") from exc
        except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.api_route(
        "/api/agents/{agent_id}/service/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy(agent_id: str, path: str, request: Request):
        agent = require(agent_id)
        if agent.service is None:
            raise HTTPException(status_code=404, detail="Agent has no service runtime")
        if supervisor.status(agent_id).status is not AgentState.RUNNING:
            raise HTTPException(status_code=503, detail="Agent is not running")
        target_base = supervisor.status(agent_id).url
        if not target_base:
            raise HTTPException(status_code=503, detail="service deployment has no proxy URL")
        target = f"{target_base.rstrip('/')}/{path}"
        headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP_BY_HOP}
        body = await request.body()
        client = httpx.AsyncClient(timeout=None)
        upstream_request = client.build_request(
            request.method, target, params=request.query_params, headers=headers, content=body
        )
        upstream = await client.send(upstream_request, stream=True)
        response_headers = {
            key: value for key, value in upstream.headers.items() if key.lower() not in HOP_BY_HOP
        }

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            stream(), status_code=upstream.status_code,
            headers=response_headers, media_type=upstream.headers.get("content-type"),
        )

    return app


def _degraded_app(instance_lock: InstanceLock, failure: Exception) -> FastAPI:
    """Keep diagnostics available without touching a failed state database."""
    reason = f"state database unavailable: {failure}"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try: yield
        finally: instance_lock.release()

    app = FastAPI(title="Agent Harness (degraded)", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def reject_mutations(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            return Response(content=json.dumps({"detail": reason}), status_code=503, media_type="application/json")
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]: return {"status": "degraded"}

    @app.get("/ready")
    async def ready(): raise HTTPException(status_code=503, detail={"ready": False, "reasons": [reason]})

    @app.get("/api/system/status")
    async def status() -> dict[str, Any]:
        return {"ready": False, "reasons": [reason], "instance": {"pid": os.getpid(), "lock_held": instance_lock.held}}

    return app
