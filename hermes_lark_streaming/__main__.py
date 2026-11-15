"""Native plugin management and read-only compatibility diagnostics."""

from __future__ import annotations

import argparse
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PLUGIN_NAME = "hermes-lark-streaming"


def native_status() -> dict[str, Any]:
    from .native_plugin import NATIVE_HOOKS, legacy_files

    try:
        from gateway.run import GatewayRunner  # type: ignore[import-not-found]
        from hermes_cli import plugins  # type: ignore[import-not-found]
    except ImportError as error:
        return {"compatible": False, "error": str(error)}
    root = Path(inspect.getfile(GatewayRunner)).resolve().parent.parent
    from .config import hermes_home
    from .native_runtime import running_adapter

    residue = legacy_files(root)
    missing = sorted(set(NATIVE_HOOKS) - plugins.VALID_HOOKS)
    compatible = not missing and callable(getattr(plugins.PluginContext, "register_platform", None)) and not residue
    result: dict[str, Any] = {
        "compatible": compatible,
        "host": str(root),
        "legacy_injections": [str(path) for path in residue],
        "missing_hooks": missing,
        "registered_adapter": None,
        "plugin_enabled": False,
        "running_adapter": running_adapter(hermes_home()),
    }
    if compatible:
        from gateway.platform_registry import platform_registry  # type: ignore[import-not-found]

        manager = plugins.get_plugin_manager()
        manager.discover_and_load()
        entry = platform_registry.get("feishu")
        if entry is not None:
            factory = entry.adapter_factory
            result["registered_adapter"] = f"{factory.__module__}.{factory.__qualname__}"
        result["plugin_enabled"] = bool(
            entry is not None
            and entry.plugin_name == PLUGIN_NAME
            and getattr(entry.adapter_factory, "__name__", "") == "FeishuStreamingAdapter"
        )
    return result


def manager_command(*args: str) -> int:
    return subprocess.call([sys.executable, "-m", "hermes_cli.main", "plugins", *args])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("verify", "status", "install", "uninstall", "legacy-clean", "migrate-config")
    )
    parser.add_argument("--apply", action="store_true", help="Apply the displayed configuration migration")
    args = parser.parse_args()
    if args.command == "migrate-config":
        from hermes_cli.config import read_raw_config, save_config  # type: ignore[import-not-found]

        from .migration import prepare_config

        updated, changes = prepare_config(read_raw_config())
        print(json.dumps(changes, indent=2, ensure_ascii=False))
        if args.apply and changes:
            save_config(updated, strip_defaults=False)
        return 0
    if args.command == "legacy-clean":
        from .legacy_cli import _cmd_uninstall

        return _cmd_uninstall()
    if args.command in {"verify", "status"}:
        report = native_status()
        print(f"Native interfaces: {'compatible' if report['compatible'] else 'unavailable'}")
        if report.get("error"):
            print(report["error"])
        print(f"Host: {report.get('host', 'not installed')}")
        print(f"Legacy injection files: {len(report.get('legacy_injections', []))}")
        for path in report.get("legacy_injections", []):
            print(f"  {path}")
        if report.get("missing_hooks"):
            print("Missing hooks: " + ", ".join(report["missing_hooks"]))
        print(f"Adapter: {report.get('registered_adapter') or 'not loaded'}")
        print(f"Plugin enabled: {report.get('plugin_enabled', False)}")
        print(f"Running adapter: {report.get('running_adapter') or 'not reported by a live gateway'}")
        return 0 if report["compatible"] else 1
    if args.command == "uninstall":
        return manager_command("remove", PLUGIN_NAME)
    from .native_plugin import check_host

    check_host()
    source = Path(__file__).resolve().parent.parent
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True)
    if dirty.strip():
        print("Commit the reviewed local plugin changes before installing, so the manager installs that exact tree.")
        return 1
    return manager_command("install", source.as_uri(), "--enable", "--yes-deps")


if __name__ == "__main__":
    raise SystemExit(main())
