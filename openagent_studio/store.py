from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Any
import yaml
from .models import ProjectSpec


class SpecStore:
    def __init__(self, path: Path):
        self.path, self._lock = path, RLock()

    def load(self) -> ProjectSpec:
        if not self.path.exists():
            return ProjectSpec(name="Untitled")
        with self._lock:
            return ProjectSpec.model_validate(yaml.safe_load(self.path.read_text()) or {})

    def raw(self) -> dict[str, Any]:
        with self._lock:
            return yaml.safe_load(self.path.read_text()) if self.path.exists() else {}

    def etag(self) -> str:
        return hashlib.sha256(self.path.read_bytes() if self.path.exists() else b"").hexdigest()

    def save(self, spec: ProjectSpec, expected: str | None = None) -> str:
        with self._lock:
            if expected is not None and expected != self.etag():
                raise ValueError("configuration changed; reload before saving")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = yaml.safe_dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)
            return self.etag()
