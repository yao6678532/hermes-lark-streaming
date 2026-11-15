#!/usr/bin/env python3
"""Prepare disposable test/lint interpreters via the pinned host's official PM."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.source.resolve()
    actual = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if actual != args.expected_revision:
        raise SystemExit("Host commit changed before PM preparation")
    plugin = Path(__file__).resolve().parent.parent
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "user").mkdir(exist_ok=True)
    os.environ["HOME"] = str(out / "user")
    os.environ["HERMES_HOME"] = str(out / "home")
    os.environ["HERMES_RUNTIME_DIR"] = str(out / "tools")
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    sys.path.insert(0, str(root))
    import pm

    project = tomllib.loads((plugin / "pyproject.toml").read_text())["project"]
    runtime = out / "host-requirements.txt"
    pm.export_requirements(root, runtime, extras=["feishu"], python=Path(sys.executable), explicit=True)
    locked = [line for line in runtime.read_text().splitlines() if line and not line.startswith("#")]
    test_python = pm.build_requirements_environment(
        [*locked, *project["dependencies"], *project["optional-dependencies"]["dev"]],
        out=out / "test",
        lock=root / "uv.lock",
        explicit=True,
    )
    # Separate lint tooling from the runtime under test, using the host's httpx pin.
    host_deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    declared = [*project["optional-dependencies"]["dev"], *project["dependencies"]]
    declared.extend(dep for dep in host_deps if dep.startswith("httpx["))
    lint_python = pm.build_requirements_environment(declared, out=out / "lint", explicit=True)
    outputs = {"test_python": str(test_python), "lint_python": str(lint_python), "host_revision": actual}
    (out / "interpreters.json").write_text(json.dumps(outputs, indent=2))
    if dest := os.environ.get("GITHUB_OUTPUT"):
        with open(dest, "a") as stream:
            for key, value in outputs.items():
                stream.write(f"{key}={value}\n")
    print(json.dumps(outputs, indent=2))


if __name__ == "__main__":
    main()
