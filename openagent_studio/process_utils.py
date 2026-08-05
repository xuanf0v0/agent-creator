from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Mapping


def resolve_executable(command: str, environment: Mapping[str, str] | None = None) -> str:
    environment = environment or os.environ
    expanded = str(Path(command).expanduser())
    resolved = shutil.which(expanded, path=environment.get("PATH"))
    if resolved:
        return resolved
    # Resolve Windows launchers deterministically even when validation runs on
    # another platform (for example a macOS CI host preparing Windows config).
    if environment.get("PATHEXT") and not Path(expanded).suffix:
        directories = environment.get("PATH", "").split(os.pathsep)
        extensions = [item for item in environment["PATHEXT"].split(";") if item]
        for directory in directories:
            for extension in extensions:
                candidate = Path(directory or ".") / f"{expanded}{extension}"
                if candidate.is_file():
                    return str(candidate)
    raise FileNotFoundError(f"找不到可执行程序：{command}。请安装该程序或设置 OPENCODE_BIN 为完整路径")
