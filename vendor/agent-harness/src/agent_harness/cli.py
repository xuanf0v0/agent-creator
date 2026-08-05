from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import httpx
import typer

from .api import create_app
from .catalog import AgentCatalog, ManifestError, configured_allowed_roots
from .instance_lock import InstanceLockedError

app = typer.Typer(help="Manage declarative local HTTP agents.", no_args_is_help=True)
DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_MANIFESTS = Path.cwd() / "agents"


def _request(method: str, path: str, base_url: str = DEFAULT_URL) -> object:
    try:
        token = os.environ.get("AGENT_HARNESS_TOKEN", "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = httpx.request(method, f"{base_url.rstrip('/')}{path}", headers=headers, timeout=None)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        typer.echo(exc.response.text, err=True)
        raise typer.Exit(1) from exc
    except httpx.HTTPError as exc:
        typer.echo(f"Cannot reach harness: {exc}", err=True)
        raise typer.Exit(1) from exc


def _show(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@app.command()
def validate(manifest_dir: Annotated[Path, typer.Argument()] = DEFAULT_MANIFESTS) -> None:
    """Validate YAML manifests without starting the service."""
    try:
        catalog = AgentCatalog.load(manifest_dir, configured_allowed_roots())
    except ManifestError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Valid: {len(catalog.all())} agent(s)")


@app.command()
def serve(
    manifest_dir: Annotated[Path, typer.Option("--manifests", "-m")] = DEFAULT_MANIFESTS,
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8765,
) -> None:
    """Run the local management API."""
    if host not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get("AGENT_HARNESS_TOKEN"):
        typer.echo("Refusing non-local bind: authentication is not enabled", err=True)
        raise typer.Exit(2)
    import uvicorn

    try:
        harness_app = create_app(manifest_dir, lock_address=f"{host}:{port}", allowed_roots=configured_allowed_roots())
    except InstanceLockedError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    uvicorn.run(harness_app, host=host, port=port)


@app.command()
def instructions(agent_id: str, path: Annotated[str, typer.Option()] = ".", base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    _show(_request("GET", f"/api/agents/{agent_id}/instructions?relative_path={path}", base_url))


@app.command()
def environment(agent_id: str, base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    _show(_request("GET", f"/api/agents/{agent_id}/environment", base_url))


task_app = typer.Typer(help="Create and manage governed coding tasks.", no_args_is_help=True)
app.add_typer(task_app, name="task")


@task_app.command("create")
def task_create(agent_id: str, title: str, prompt: str, path: Annotated[str, typer.Option()] = ".", idempotency_key: Annotated[str | None, typer.Option()] = None, base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    try:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        if os.environ.get("AGENT_HARNESS_TOKEN"): headers["Authorization"] = f"Bearer {os.environ['AGENT_HARNESS_TOKEN']}"
        response = httpx.post(f"{base_url.rstrip('/')}/api/tasks", json={"agent_id": agent_id, "title": title, "prompt": prompt, "relative_path": path}, headers=headers, timeout=None)
        response.raise_for_status(); _show(response.json())
    except httpx.HTTPError as exc:
        typer.echo(str(exc), err=True); raise typer.Exit(1) from exc


@task_app.command("list")
def task_list(agent_id: Annotated[str | None, typer.Option()] = None, base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    suffix = f"?agent_id={agent_id}" if agent_id else ""
    _show(_request("GET", f"/api/tasks{suffix}", base_url))


@task_app.command("show")
def task_show(task_id: str, base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    _show(_request("GET", f"/api/tasks/{task_id}", base_url))


@task_app.command("logs")
def task_logs(task_id: str, lines: Annotated[int, typer.Option("--lines", "-n")] = 500, base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    for item in _request("GET", f"/api/tasks/{task_id}/logs?lines={lines}", base_url):  # type: ignore[union-attr]
        typer.echo(f"[{item['stream']}] {item['line']}")


@task_app.command("retry")
def task_retry(task_id: str, idempotency_key: Annotated[str | None, typer.Option()] = None, base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    try:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        if os.environ.get("AGENT_HARNESS_TOKEN"): headers["Authorization"] = f"Bearer {os.environ['AGENT_HARNESS_TOKEN']}"
        response = httpx.post(f"{base_url.rstrip('/')}/api/tasks/{task_id}/retry", headers=headers, timeout=None)
        response.raise_for_status(); _show(response.json())
    except httpx.HTTPError as exc:
        typer.echo(str(exc), err=True); raise typer.Exit(1) from exc


@task_app.command("cancel")
def task_cancel(task_id: str, base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    _show(_request("POST", f"/api/tasks/{task_id}/cancel", base_url))


@app.command("list")
def list_agents(base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    _show(_request("GET", "/api/agents", base_url))


def _action(agent_id: str, operation: str, base_url: str) -> None:
    _show(_request("POST", f"/api/agents/{agent_id}/{operation}", base_url))


@app.command()
def setup(agent_id: str, base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    _action(agent_id, "setup", base_url)


@app.command()
def start(agent_id: str, base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    _action(agent_id, "start", base_url)


@app.command()
def stop(agent_id: str, base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    _action(agent_id, "stop", base_url)


@app.command()
def restart(agent_id: str, base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    _action(agent_id, "restart", base_url)


@app.command()
def logs(
    agent_id: str,
    lines: Annotated[int, typer.Option("--lines", "-n")] = 200,
    base_url: Annotated[str, typer.Option()] = DEFAULT_URL,
) -> None:
    result = _request("GET", f"/api/agents/{agent_id}/logs?lines={lines}", base_url)
    for line in result.get("lines", []):  # type: ignore[union-attr]
        typer.echo(line)


@app.command()
def maintain(base_url: Annotated[str, typer.Option()] = DEFAULT_URL) -> None:
    _show(_request("POST", "/api/system/maintain", base_url))


@app.command()
def doctor(
    manifest_dir: Annotated[Path, typer.Option("--manifests", "-m")] = DEFAULT_MANIFESTS,
    base_url: Annotated[str, typer.Option()] = DEFAULT_URL,
) -> None:
    try:
        _show(_request("GET", "/api/system/status", base_url))
        return
    except typer.Exit:
        pass
    result: dict[str, object] = {"service": "unreachable"}
    try:
        result["manifests"] = {"valid": True, "agents": len(AgentCatalog.load(manifest_dir, configured_allowed_roots()).all())}
    except Exception as exc:
        result["manifests"] = {"valid": False, "error": str(exc)}
    import shutil
    import sqlite3
    disk = shutil.disk_usage(manifest_dir.resolve().parent)
    result["disk"] = {"free_bytes": disk.free, "ok": disk.free >= 256 * 1024 * 1024}
    home = Path(os.environ.get("AGENT_HARNESS_HOME", Path.home() / ".agent-harness"))
    state_path = home / "state.db"
    if state_path.exists():
        try:
            db = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
            try: result["database"] = {"path": str(state_path), "quick_check": db.execute("PRAGMA quick_check").fetchone()[0]}
            finally: db.close()
        except sqlite3.DatabaseError as exc:
            result["database"] = {"path": str(state_path), "error": str(exc)}
    _show(result)
