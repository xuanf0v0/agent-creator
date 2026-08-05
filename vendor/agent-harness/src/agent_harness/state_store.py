from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar
from uuid import uuid4

SCHEMA_VERSION = 5
T = TypeVar("T")


class StateConflict(RuntimeError):
    pass


class StateUnavailable(RuntimeError):
    pass


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self._lock, self.ready_error = path, threading.RLock(), ""
        self.last_maintenance_at: float | None = None
        self.last_cleanup_count = 0
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        check = self.quick_check()
        if check != "ok":
            self.ready_error = f"database quick_check failed: {check}"
            raise StateUnavailable(self.ready_error)
        self._recover_interrupted()

    def _migrate(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise StateUnavailable(f"database schema {version} is newer than supported {SCHEMA_VERSION}")
        has_tables = bool(self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone())
        if version < SCHEMA_VERSION and has_tables:
            backup = self.path.with_name(f"{self.path.name}.backup-v{version}-{int(time.time())}")
            target = sqlite3.connect(backup)
            try: self._db.backup(target)
            finally: target.close()
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS environments (
                  agent_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                  files_json TEXT NOT NULL, prepared_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, workspace TEXT NOT NULL,
                  relative_path TEXT NOT NULL, title TEXT NOT NULL, prompt TEXT NOT NULL,
                  status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  blocked_reason TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS attempts (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                  number INTEGER NOT NULL, status TEXT NOT NULL, instruction_text TEXT NOT NULL,
                  instruction_hash TEXT NOT NULL, instruction_sources TEXT NOT NULL,
                  started_at REAL NOT NULL, finished_at REAL, exit_code INTEGER,
                  evidence_json TEXT NOT NULL DEFAULT '[]', UNIQUE(task_id, number));
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                  kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                  attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                  stream TEXT NOT NULL, line TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS idempotency (
                  scope TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
                  resource_id TEXT NOT NULL, created_at REAL NOT NULL,
                  PRIMARY KEY(scope, key));
                CREATE TABLE IF NOT EXISTS environment_files (
                  agent_id TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL,
                  PRIMARY KEY(agent_id,path));
                CREATE TABLE IF NOT EXISTS setup_operations (
                  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, status TEXT NOT NULL,
                  fingerprint TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
                  error_message TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL,
                  finished_at REAL, request_key TEXT, logs_json TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS audit_log (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
                  method TEXT NOT NULL, path TEXT NOT NULL, status_code INTEGER NOT NULL,
                  created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS system_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                  payload_json TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_tasks_agent_status ON tasks(agent_id,status,created_at);
                CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id,number);
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id,id);
                CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id,id);
                CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);
                """
            )
            setup_columns = {row[1] for row in self._db.execute("PRAGMA table_info(setup_operations)")}
            if "logs_json" not in setup_columns:
                self._db.execute("ALTER TABLE setup_operations ADD COLUMN logs_json TEXT NOT NULL DEFAULT '[]'")
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _recover_interrupted(self) -> None:
        now = time.time()
        with self._transaction() as db:
            rows = db.execute("SELECT id FROM tasks WHERE status IN ('preparing','working','verifying')").fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET status='failed', blocked_reason='harness interrupted', updated_at=? WHERE id=?", (now, row["id"]))
                db.execute("INSERT INTO events(task_id,kind,payload_json,created_at) VALUES (?,?,?,?)", (row["id"], "interrupted", '{}', now))
                db.execute("INSERT INTO system_events(kind,payload_json,created_at) VALUES (?,?,?)", ("task.failed", json.dumps({"task_id": row["id"], "reason": "harness interrupted"}), now))
            db.execute("UPDATE attempts SET status='interrupted', finished_at=? WHERE status IN ('preparing','working','verifying')", (now,))
            setup_rows = db.execute("SELECT id,agent_id FROM setup_operations WHERE status IN ('queued','preparing')").fetchall()
            db.execute("UPDATE setup_operations SET status='error',error_code='harness_interrupted',error_message='harness restarted during setup',finished_at=? WHERE status IN ('queued','preparing')", (now,))
            for row in setup_rows:
                db.execute("INSERT INTO system_events(kind,payload_json,created_at) VALUES (?,?,?)", ("setup.error", json.dumps({"operation_id": row["id"], "agent_id": row["agent_id"], "error_code": "harness_interrupted"}), now))

    def _transaction(self):
        return self._db

    def _retry(self, operation: Callable[[], T]) -> T:
        delay = 0.02
        for attempt in range(4):
            try: return operation()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower(): raise
                if attempt == 3: raise StateUnavailable("database remained busy") from exc
                time.sleep(delay); delay *= 2
        raise AssertionError("unreachable")

    def quick_check(self) -> str:
        try: return str(self._db.execute("PRAGMA quick_check").fetchone()[0])
        except sqlite3.DatabaseError as exc: return str(exc)

    def close(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()

    def set_environment(self, agent_id: str, value: str, files: list[str], hashes: dict[str, str] | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO environments VALUES (?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET fingerprint=excluded.fingerprint,files_json=excluded.files_json,prepared_at=excluded.prepared_at", (agent_id, value, json.dumps(files), time.time()))
            if hashes is not None:
                self._db.execute("DELETE FROM environment_files WHERE agent_id=?", (agent_id,))
                self._db.executemany("INSERT INTO environment_files(agent_id,path,digest) VALUES (?,?,?)", ((agent_id, path, digest) for path, digest in hashes.items()))

    def environment(self, agent_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM environments WHERE agent_id=?", (agent_id,)).fetchone()
        if not row: return None
        value = dict(row); value["files"] = json.loads(value.pop("files_json"))
        value["file_hashes"] = {item["path"]: item["digest"] for item in self._db.execute("SELECT path,digest FROM environment_files WHERE agent_id=?", (agent_id,))}
        return value

    def start_setup(self, agent_id: str, request_key: str | None = None) -> dict[str, Any]:
        with self._lock, self._db:
            if request_key:
                row = self._db.execute("SELECT * FROM setup_operations WHERE agent_id=? AND request_key=? ORDER BY started_at DESC LIMIT 1", (agent_id, request_key)).fetchone()
                if row:
                    value = self._setup_value(row); value["_created"] = False; return value
            active = self._db.execute("SELECT * FROM setup_operations WHERE agent_id=? AND status IN ('queued','preparing') ORDER BY started_at DESC LIMIT 1", (agent_id,)).fetchone()
            if active:
                value = self._setup_value(active); value["_created"] = False; return value
            operation_id, now = uuid4().hex, time.time()
            self._db.execute("INSERT INTO setup_operations(id,agent_id,status,started_at,request_key) VALUES (?,?,?,?,?)", (operation_id, agent_id, "queued", now, request_key))
        value = self.setup_operation(operation_id); value["_created"] = True; return value

    def update_setup(self, operation_id: str, status: str, fingerprint: str = "", error_code: str = "", error_message: str = "", logs: list[str] | None = None) -> dict[str, Any]:
        finished = time.time() if status in {"ready", "error"} else None
        with self._lock, self._db:
            if logs is None:
                self._db.execute("UPDATE setup_operations SET status=?,fingerprint=?,error_code=?,error_message=?,finished_at=? WHERE id=?", (status, fingerprint, error_code, error_message, finished, operation_id))
            else:
                self._db.execute("UPDATE setup_operations SET status=?,fingerprint=?,error_code=?,error_message=?,finished_at=?,logs_json=? WHERE id=?", (status, fingerprint, error_code, error_message, finished, json.dumps(logs), operation_id))
        operation = self.setup_operation(operation_id)
        self.add_system_event(f"setup.{status}", {"operation_id": operation_id, "agent_id": operation["agent_id"], "error_code": error_code})
        return operation

    @staticmethod
    def _setup_value(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["logs"] = json.loads(value.pop("logs_json", "[]"))
        return value

    def setup_operation(self, operation_id: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM setup_operations WHERE id=?", (operation_id,)).fetchone()
        if not row: raise KeyError(operation_id)
        return self._setup_value(row)

    def latest_setup(self, agent_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM setup_operations WHERE agent_id=? ORDER BY started_at DESC LIMIT 1", (agent_id,)).fetchone()
        return self._setup_value(row) if row else None

    def add_audit(self, actor: str, method: str, path: str, status_code: int) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO audit_log(actor,method,path,status_code,created_at) VALUES (?,?,?,?,?)", (actor, method, path, status_code, time.time()))

    def audit_entries(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def add_system_event(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO system_events(kind,payload_json,created_at) VALUES (?,?,?)", (kind, json.dumps(payload), time.time()))

    def system_events(self, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        result = []
        for row in self._db.execute("SELECT id,kind,payload_json,created_at FROM system_events WHERE id>? ORDER BY id LIMIT ?", (after_id, limit)):
            item = dict(row); item["payload"] = json.loads(item.pop("payload_json")); result.append(item)
        return result

    @staticmethod
    def request_hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def idempotent_resource(self, scope: str, key: str, request_hash: str) -> str | None:
        row = self._db.execute("SELECT request_hash,resource_id FROM idempotency WHERE scope=? AND key=?", (scope, key)).fetchone()
        if not row: return None
        if row["request_hash"] != request_hash: raise StateConflict("Idempotency-Key was reused with a different request")
        return str(row["resource_id"])

    def create_task(self, agent_id: str, workspace: Path, relative_path: str, title: str, prompt: str, idempotency_key: str | None = None) -> dict[str, Any]:
        payload = {"agent_id": agent_id, "workspace": str(workspace), "relative_path": relative_path, "title": title, "prompt": prompt}
        digest = self.request_hash(payload)
        if idempotency_key:
            existing = self.idempotent_resource("task.create", idempotency_key, digest)
            if existing: return self.task(existing)
        task_id, now = uuid4().hex, time.time()
        with self._lock, self._db:
            self._db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)", (task_id, agent_id, str(workspace), relative_path, title, prompt, "queued", now, now, ""))
            self._event(self._db, task_id, "queued", {}, now)
            if idempotency_key:
                try: self._db.execute("INSERT INTO idempotency VALUES (?,?,?,?,?)", ("task.create", idempotency_key, digest, task_id, now))
                except sqlite3.IntegrityError as exc: raise StateConflict("concurrent idempotent request") from exc
        self.add_system_event("task.queued", {"task_id": task_id, "agent_id": agent_id})
        return self.task(task_id)

    def task(self, task_id: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row: raise KeyError(task_id)
        value = dict(row)
        value["error_code"] = "setup_required" if value["status"] == "blocked" and value["blocked_reason"] == "environment drift; run setup" else ("task_failed" if value["status"] == "failed" else "")
        value["setup_required"] = value["error_code"] == "setup_required"
        value["task_retry_count"] = max(0, int(self._db.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task_id,)).fetchone()[0]) - 1)
        value["attempts"] = [dict(item) for item in self._db.execute("SELECT * FROM attempts WHERE task_id=? ORDER BY number", (task_id,))]
        for attempt in value["attempts"]:
            attempt["instruction_sources"] = json.loads(attempt["instruction_sources"])
            attempt["evidence"] = json.loads(attempt.pop("evidence_json")); attempt.pop("instruction_text", None)
        return value

    def tasks(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        query, args = "SELECT id FROM tasks", ()
        if agent_id: query, args = f"{query} WHERE agent_id=?", (agent_id,)
        return [self.task(row["id"]) for row in self._db.execute(query + " ORDER BY created_at DESC", args)]

    def queue_depth(self, agent_id: str) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM tasks WHERE agent_id=? AND status='queued'", (agent_id,)).fetchone()[0])

    def transition(self, task_id: str, expected: set[str], status: str, reason: str = "") -> None:
        with self._lock, self._db:
            placeholders = ",".join("?" for _ in expected)
            cursor = self._db.execute(f"UPDATE tasks SET status=?,blocked_reason=?,updated_at=? WHERE id=? AND status IN ({placeholders})", (status, reason, time.time(), task_id, *expected))
            if cursor.rowcount != 1: raise StateConflict(f"task is not in an allowed state: {sorted(expected)}")
            self._event(self._db, task_id, status, {"reason": reason} if reason else {})
        self.add_system_event(f"task.{status}", {"task_id": task_id, "reason": reason})

    def set_task_status(self, task_id: str, status: str, reason: str = "") -> None:
        with self._lock, self._db:
            cursor = self._db.execute("UPDATE tasks SET status=?,blocked_reason=?,updated_at=? WHERE id=?", (status, reason, time.time(), task_id))
            if cursor.rowcount != 1: raise KeyError(task_id)
            self._event(self._db, task_id, status, {"reason": reason} if reason else {})
        self.add_system_event(f"task.{status}", {"task_id": task_id, "reason": reason})

    def start_attempt(self, task_id: str, instruction_text: str, instruction_hash: str, sources: tuple[str, ...]) -> str:
        attempt_id, now = uuid4().hex, time.time()
        with self._lock, self._db:
            number = self._db.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task_id,)).fetchone()[0] + 1
            self._db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, task_id, number, "preparing", instruction_text, instruction_hash, json.dumps(sources), now, None, None, "[]"))
        return attempt_id

    def finish_attempt(self, attempt_id: str, status: str, exit_code: int | None, evidence: list[dict[str, Any]]) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE attempts SET status=?,finished_at=?,exit_code=?,evidence_json=? WHERE id=?", (status, time.time(), exit_code, json.dumps(evidence), attempt_id))

    def set_attempt_status(self, attempt_id: str, status: str) -> None:
        with self._lock, self._db: self._db.execute("UPDATE attempts SET status=? WHERE id=?", (status, attempt_id))

    def add_logs(self, rows: list[tuple[str, str, str, str, float]]) -> None:
        if not rows: return
        with self._lock, self._db:
            self._db.executemany("INSERT INTO logs(task_id,attempt_id,stream,line,created_at) VALUES (?,?,?,?,?)", rows)

    def add_log(self, task_id: str, attempt_id: str, stream: str, line: str) -> None:
        self.add_logs([(task_id, attempt_id, stream, line, time.time())])

    def logs(self, task_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT attempt_id,stream,line,created_at FROM logs WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    @staticmethod
    def _event(db: sqlite3.Connection, task_id: str, kind: str, payload: dict[str, Any], created_at: float | None = None) -> None:
        db.execute("INSERT INTO events(task_id,kind,payload_json,created_at) VALUES (?,?,?,?)", (task_id, kind, json.dumps(payload), created_at or time.time()))

    def add_event(self, task_id: str, kind: str, payload: dict[str, Any]) -> None:
        with self._lock, self._db: self._event(self._db, task_id, kind, payload)

    def events(self, task_id: str) -> list[dict[str, Any]]:
        result = []
        for row in self._db.execute("SELECT id,kind,payload_json,created_at FROM events WHERE task_id=? ORDER BY id", (task_id,)):
            item = dict(row); item["payload"] = json.loads(item.pop("payload_json")); result.append(item)
        return result

    def maintain(self, retention_days: int = 30) -> dict[str, Any]:
        cutoff = time.time() - retention_days * 86400
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM logs WHERE created_at<? AND task_id IN (SELECT id FROM tasks WHERE status IN ('completed','failed','cancelled'))", (cutoff,))
            count = cursor.rowcount
        self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        self.last_maintenance_at, self.last_cleanup_count = time.time(), count
        return {"logs_deleted": count, "maintained_at": self.last_maintenance_at, "quick_check": self.quick_check()}

    def write_progress(self, workspace: Path) -> bool:
        rows = self._db.execute("SELECT id,title,status,blocked_reason FROM tasks WHERE workspace=? ORDER BY created_at DESC", (str(workspace),)).fetchall()
        lines = ["# Harness Progress", "", "Generated from the local harness state database.", ""]
        for row in rows:
            detail = f" — {row['blocked_reason']}" if row["blocked_reason"] else ""
            lines.append(f"- [{row['status']}] {row['title']} (`{row['id']}`){detail}")
        try:
            directory = workspace / ".harness"; directory.mkdir(parents=True, exist_ok=True)
            descriptor, temp = tempfile.mkstemp(dir=directory, prefix=".PROGRESS.")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as file: file.write("\n".join(lines) + "\n")
                os.replace(temp, directory / "PROGRESS.md")
            finally:
                if os.path.exists(temp): os.unlink(temp)
            return True
        except OSError:
            return False
