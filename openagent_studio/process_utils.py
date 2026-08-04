from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Mapping


def resolve_executable(command: str, environment: Mapping[str, str] | None = None) -> str:
    expanded = str(Path(command).expanduser())
    resolved = shutil.which(expanded, path=(environment or os.environ).get("PATH"))
    if resolved:
        return resolved
    raise FileNotFoundError(f"找不到可执行程序：{command}。请安装该程序或设置 OPENCODE_BIN 为完整路径")
