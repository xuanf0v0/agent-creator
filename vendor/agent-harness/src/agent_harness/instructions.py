from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstructionBundle:
    content: str
    sha256: str
    sources: tuple[str, ...]


def safe_task_directory(root: Path, relative_path: str = ".") -> Path:
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("task path escapes agent workspace") from exc
    if not candidate.is_dir():
        raise ValueError(f"task directory does not exist: {relative_path}")
    return candidate


def load_instructions(root: Path, task_dir: Path) -> InstructionBundle:
    root = root.resolve()
    task_dir = safe_task_directory(root, str(task_dir.resolve().relative_to(root)))
    candidates: list[Path] = [root / "AGENTS.md", root / "CLAUDE.md"]
    relative = task_dir.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor /= part
        candidates.append(cursor / "AGENTS.md")
    sources: list[str] = []
    sections: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        source = str(path.relative_to(root))
        sources.append(source)
        sections.append(f"<!-- source: {source} -->\n{path.read_text(encoding='utf-8').strip()}")
    content = "\n\n".join(sections)
    return InstructionBundle(
        content=content,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        sources=tuple(sources),
    )
