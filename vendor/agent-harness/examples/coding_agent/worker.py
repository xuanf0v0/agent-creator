from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.load(sys.stdin)
Path("task-output.txt").write_text(
    f"{payload['task']['prompt']}\n\nInstructions:\n{payload['instructions']}\n",
    encoding="utf-8",
)
print(f"completed task {payload['task']['id']}")
