from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .api import create_app
from .catalog import AgentCatalog


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-harness")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--manifests", "-m", type=Path, default=Path.cwd() / "agents")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    validate = commands.add_parser("validate")
    validate.add_argument("manifest_dir", type=Path, nargs="?", default=Path.cwd() / "agents")
    args = parser.parse_args()
    if args.command == "validate":
        catalog = AgentCatalog.load(args.manifest_dir)
        print(f"Valid: {len(catalog.all())} agent(s)")
        return
    uvicorn.run(create_app(args.manifests, lock_address=f"{args.host}:{args.port}"), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
