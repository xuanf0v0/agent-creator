from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import AgentManifest

DEFAULT_FINGERPRINT_FILES = (
    "uv.lock", "poetry.lock", "requirements.txt", "pyproject.toml",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "package.json",
    "Dockerfile", ".devcontainer/devcontainer.json", "setup.sh",
)


def fingerprint(agent: AgentManifest) -> tuple[str, list[str]]:
    value, files, _ = fingerprint_details(agent)
    return value, files


def fingerprint_details(agent: AgentManifest) -> tuple[str, list[str], dict[str, str]]:
    configured = agent.environment.fingerprint_files
    paths = configured or [agent.cwd / item for item in DEFAULT_FINGERPRINT_FILES]
    digest = hashlib.sha256()
    included: list[str] = []
    hashes: dict[str, str] = {}
    setup_payload = json.dumps(agent.environment.setup_command, ensure_ascii=False, separators=(",", ":")).encode()
    setup_hash = hashlib.sha256(setup_payload).hexdigest()
    hashes["<setup_command>"] = setup_hash
    digest.update(b"<setup_command>\0")
    digest.update(setup_payload)
    digest.update(b"\0")
    for path in sorted((item.resolve() for item in paths), key=str):
        try:
            relative = str(path.relative_to(agent.cwd))
        except ValueError as exc:
            raise ValueError(f"fingerprint path escapes workspace: {path}") from exc
        if not path.is_file():
            continue
        included.append(relative)
        content = path.read_bytes()
        hashes[relative] = hashlib.sha256(content).hexdigest()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest(), included, hashes
