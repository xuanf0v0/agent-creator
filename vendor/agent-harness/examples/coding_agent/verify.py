from pathlib import Path

content = Path("task-output.txt").read_text(encoding="utf-8")
raise SystemExit(0 if content.strip() else 1)
