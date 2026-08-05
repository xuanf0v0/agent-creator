from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from typing import Any


class PluginNotFound(ValueError):
    pass


def resolve_plugin(kind: str, group: str, builtins: dict[str, Any]) -> Any:
    """Resolve a built-in, installed entry point, or explicit module:attribute."""
    if kind in builtins:
        return builtins[kind]
    for entry in entry_points().select(group=group, name=kind):
        return entry.load()
    if ":" in kind:
        module_name, attribute = kind.split(":", 1)
        try:
            return getattr(import_module(module_name), attribute)
        except (ImportError, AttributeError) as exc:
            raise PluginNotFound(f"cannot load {group} plugin {kind}: {exc}") from exc
    raise PluginNotFound(f"unknown {group} plugin: {kind}")
