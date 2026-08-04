from __future__ import annotations

from pathlib import Path

import pytest

from openagent_studio.process_utils import resolve_executable


def test_resolve_executable_uses_windows_pathext(tmp_path: Path):
    executable = tmp_path / "opencode.CMD"
    executable.write_text("@echo off\n", encoding="utf-8")
    environment = {"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT;.CMD"}

    assert Path(resolve_executable("opencode", environment)).resolve() == executable.resolve()


def test_resolve_executable_reports_missing_program(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="OPENCODE_BIN"):
        resolve_executable("missing-opencode", {"PATH": str(tmp_path), "PATHEXT": ".EXE;.CMD"})
