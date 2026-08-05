from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .catalog import AgentCatalog
from .instance_lock import InstanceLock
from .state_store import SCHEMA_VERSION, StateStore
from .task_runner import TaskRunner


class ReliabilityManager:
    def __init__(self, home: Path, store: StateStore, runner: TaskRunner, catalog: AgentCatalog, lock: InstanceLock) -> None:
        self.home, self.store, self.runner, self.catalog = home, store, runner, catalog
        self.lock = lock
        self.started_at = time.time()
        self._maintenance_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self.shutting_down = False

    async def start(self) -> None:
        self.store.maintain()
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def shutdown(self) -> None:
        self.shutting_down = True
        for task in (self._maintenance_task, self._heartbeat_task):
            if task: task.cancel()
        await asyncio.gather(*(task for task in (self._maintenance_task, self._heartbeat_task) if task), return_exceptions=True)
        self.store.maintain()
        self.store.close()
        self.lock.release()

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(86400)
            self.store.maintain()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            self.lock.heartbeat()

    def status(self) -> dict[str, Any]:
        disk = shutil.disk_usage(self.home)
        reasons: list[str] = []
        warnings: list[str] = []
        if self.shutting_down: reasons.append("harness is shutting down")
        if self.runner.ready_error: reasons.append(self.runner.ready_error)
        check = self.store.quick_check()
        if check != "ok": reasons.append(f"database integrity: {check}")
        if disk.free < 256 * 1024 * 1024: reasons.append("less than 256 MiB disk space available")
        elif disk.free < 1024 * 1024 * 1024: warnings.append("less than 1 GiB disk space available")
        queues = {}
        for agent in self.catalog.all():
            if agent.task:
                queues[agent.id] = {"queued": self.store.queue_depth(agent.id), "active": [task_id for task_id in self.runner._active if self.store.task(task_id)["agent_id"] == agent.id]}
        return {
            "ready": not reasons, "reasons": reasons, "warnings": warnings,
            "instance": {"pid": os.getpid(), "uptime_seconds": time.time() - self.started_at, "lock_held": self.lock.held},
            "database": {"path": str(self.store.path), "schema_version": SCHEMA_VERSION, "quick_check": check, "journal_mode": "wal"},
            "disk": {"free_bytes": disk.free, "total_bytes": disk.total}, "queues": queues,
            "maintenance": {"last_at": self.store.last_maintenance_at, "last_logs_deleted": self.store.last_cleanup_count},
        }

    def require_ready(self) -> None:
        status = self.status()
        if not status["ready"]:
            raise RuntimeError("; ".join(status["reasons"]))
