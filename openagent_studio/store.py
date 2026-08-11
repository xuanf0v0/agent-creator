from __future__ import annotations

import hashlib
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

from .models import ProjectSpec


class SpecStore:
    def __init__(self, path: Path):
        self.path, self._lock = path, RLock()
        self._cached_spec: ProjectSpec | None = None
        self._cached_signature: tuple[int, int] | None = None
        self._cached_etag: str | None = None

    def _signature(self) -> tuple[int, int]:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return (0, 0)
        return (stat.st_mtime_ns, stat.st_size)

    def load(self) -> ProjectSpec:
        with self._lock:
            signature = self._signature()
            if self._cached_spec is not None and signature == self._cached_signature:
                return self._cached_spec
            if not self.path.exists():
                spec = ProjectSpec(name="Untitled")
            else:
                spec = ProjectSpec.model_validate(yaml.safe_load(self.path.read_text(encoding="utf-8")) or {})
            self._cached_spec = spec
            self._cached_signature = signature
            self._cached_etag = None
            return spec

    def raw(self) -> dict[str, Any]:
        with self._lock:
            return yaml.safe_load(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def etag(self) -> str:
        with self._lock:
            signature = self._signature()
            if signature == self._cached_signature and self._cached_etag is not None:
                return self._cached_etag
            value = hashlib.sha256(self.path.read_bytes() if self.path.exists() else b"").hexdigest()
            self._cached_signature = signature
            self._cached_etag = value
            return value

    def save(self, spec: ProjectSpec, expected: str | None = None) -> str:
        with self._lock:
            if expected is not None and expected != self.etag():
                raise ValueError("configuration changed; reload before saving")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = yaml.safe_dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)
            self._cached_spec = spec
            self._cached_signature = self._signature()
            self._cached_etag = hashlib.sha256(self.path.read_bytes()).hexdigest()
            return self._cached_etag
