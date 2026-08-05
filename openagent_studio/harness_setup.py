from __future__ import annotations

import ensurepip
import subprocess
import sys


def main() -> int:
    probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0:
        ensurepip.bootstrap(default_pip=True)
    return subprocess.call([sys.executable, "-m", "pip", "install", "-e", "."])


if __name__ == "__main__":
    raise SystemExit(main())
