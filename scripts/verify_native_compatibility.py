#!/usr/bin/env python3
"""Audit native-only migration gates against a complete, pinned Hermes runtime.

Run with the target Hermes interpreter. The worker uses a disposable HOME,
does not connect an adapter or provider, and never patches host source files.
Exit 2 means a preservation gate failed; it is not a successful upgrade check.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import warnings
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

TARGET_REVISION = "3b421578588243f920886e6fe79a6faa87ea026e"
RESULT_PREFIX = "HLS_NATIVE_AUDIT="


def host_hashes(source: Path) -> dict[str, str]:
    names = (
        "gateway/run_turn_runner.py", "gateway/run_turn_progress.py",
        "gateway/run_agent_cache.py", "agent/background_review.py", "agent/plugin_stream_hooks.py",
        "agent/inline_tool_executors.py", "agent/turn_context.py", "gateway/session_context.py",
        "hermes_cli/plugins.py", "hermes_cli/plugins_dispatch.py",
        "tools/skill_provenance.py", "tools/memory_tool.py", "tools/skill_manager_tool.py",
    )
    return {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names}


def plugin_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parent.parent
    paths = [*sorted((root / "hermes_lark_streaming").rglob("*.py")),
             root / "plugin.yaml", root / "__init__.py", root / "pyproject.toml"]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def background_review_probe() -> dict[str, Any]:
    """Compare the existing callback contract with the real native gateway."""
    from agent.background_review import _publish_review_summary
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext
    from hermes_cli import lifecycle

    from hermes_lark_streaming.patcher import _background_review_hook

    sends: list[dict[str, Any]] = []
    hook_calls: list[str] = []

    class Adapter:
        async def send(self, chat_id: str, content: str, metadata: Any = None) -> SendResult:
            sends.append({"chat_id": chat_id, "text": content})
            return SendResult(success=True, message_id="offline-audit-message")

    runner = GatewayRunner(GatewayConfig())
    key = "offline-audit-session"
    runner._session_state(key).persistent.run_generation = 1
    ctx = TurnContext(
        source=SessionSource(platform=Platform.FEISHU, chat_id="offline-audit-chat", user_id="offline-owner"),
        session_key=key, _status_adapter=Adapter(), _status_chat_id="offline-audit-chat",
        _run_still_current=runner._run_still_current_fn(key, 1),
    )
    turn = TurnRunner(runner, ctx)

    def schedule(coro: Any, _label: str) -> Future[Any]:
        result: Future[Any] = Future()
        result.set_result(asyncio.run(coro))
        return result

    turn._schedule = schedule
    send, release = turn._make_bg_review_callbacks()
    agent = SimpleNamespace(background_review_callback=send, _safe_print=Mock())
    actions = ["Memory updated", "Skill updated"]
    release()
    with patch.object(lifecycle, "invoke_hook", side_effect=lambda name, **_kw: hook_calls.append(name)):
        _publish_review_summary(agent, actions)
        assert len(sends) == 1, "The control case must actually reach the adapter"
        live_text = sends[0]["text"]
        sends.clear()
        # The same chat has started its next turn. Use the host's actual
        # generation predicate, rather than mocking _status_live to False.
        runner._session_state(key).persistent.run_generation = 2
        assert not ctx._run_still_current()
        _publish_review_summary(agent, actions)
        native_late = len(sends)
    published = Mock(return_value=True)
    with patch("hermes_lark_streaming.patch.on_background_review_message", published):
        # Execute our existing generated hook in a disposable namespace. This
        # changes only this fake agent callback, never a host method or file.
        namespace = {"agent": agent, "ctx": ctx, "self": turn}
        exec(_background_review_hook(""), namespace)
        _publish_review_summary(agent, actions)
    assert published.call_count == 1, "Legacy control must preserve the late summary"
    assert published.call_args.kwargs["text"] == live_text
    assert published.call_args.kwargs["conversation_key"] == "feishu:offline-audit-chat"
    return {
        "native_current_generation_deliveries": 1,
        "native_late_summary_deliveries": native_late,
        "legacy_late_summary_deliveries": published.call_count,
        "summary_plugin_hooks_fired": hook_calls,
        "preserved": native_late == published.call_count,
    }


def stream_observer_probe() -> dict[str, Any]:
    """Deterministically exercise the real producer and bounded observer queue."""
    from agent import plugin_stream_hooks as streams
    from hermes_cli import plugins
    from run_agent import AIAgent

    entered, release, drained = threading.Event(), threading.Event(), threading.Event()
    direct: list[str] = []
    observed: list[str] = []
    count = streams._QUEUE_SIZE + 8

    def receive(**payload: Any) -> None:
        delta = payload["delta"]
        if delta == "first":
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Audit worker was not released")
        observed.append(delta)
        if delta == f"delta-{count - 1}":
            drained.set()

    agent = AIAgent.__new__(AIAgent)
    agent.reasoning_callback = direct.append
    agent._stream_reasoning_hooks_enabled = True
    agent.session_id = "offline-audit-session"
    agent.model = "offline-audit-model"
    agent.provider = "offline-audit-provider"
    agent.platform = "feishu"
    agent._current_turn_id = "offline-audit-turn"
    with patch.object(
        plugins, "iter_hook_callbacks",
        side_effect=lambda name: (receive,) if name == "on_stream_delta" else (),
    ):
        try:
            agent._fire_reasoning_delta("first")
            assert entered.wait(5), "Real observer thread failed to start"
            for index in range(count):
                agent._fire_reasoning_delta(f"delta-{index}")
            release.set()
            assert drained.wait(5), "Real observer thread failed to drain"
        finally:
            release.set()
            streams.shutdown_plugin_stream_hook_dispatcher()
    assert len(direct) == count + 1
    assert len(observed) == streams._QUEUE_SIZE + 1
    return {
        "emitted_and_direct_callback_received": len(direct),
        "official_observer_received": len(observed),
        "dropped": len(direct) - len(observed),
        "queue_capacity": streams._QUEUE_SIZE,
        "assessment": "risk; final reconciliation needs independent authoritative snapshots",
    }


def worker(source: Path, probe: str = "baseline", expected_revision: str = TARGET_REVISION) -> dict[str, Any]:
    import socket

    warnings.simplefilter("always", RuntimeWarning)
    before = host_hashes(source)
    plugin_before = plugin_hashes()
    with (
        patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden in audit")) as connect,
        patch.object(
            socket.socket, "connect_ex", side_effect=AssertionError("Network forbidden in audit"),
        ) as connect_ex,
    ):
        if probe == "regression":
            import pytest

            fixture_source = os.environ["HLS_AUDIT_FIXTURE_SOURCE"]
            fixture_root = Path(os.environ["HERMES_HOME"]) / "hermes-agent"
            subprocess.run(
                ["git", "clone", "--shared", "--no-checkout", fixture_source, str(fixture_root)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(fixture_root), "checkout", "--detach",
                 "71d34bad1045610252eda02ee79549436896e784"], check=True, capture_output=True,
            )
            results: dict[str, Any] = {}

            class Collect:
                def pytest_terminal_summary(self, terminalreporter: Any) -> None:
                    results.update({key: len(value) for key, value in terminalreporter.stats.items() if key})

            test_dir = Path(__file__).resolve().parent.parent / "tests"
            code = pytest.main([str(test_dir), "-q", "-ra"], plugins=[Collect()])
            regression = {"exit_code": int(code), "counts": results}
        elif probe == "platform":
            from native_platform_probe import run_platform_probe

            platform = run_platform_probe()
        elif probe == "tool-receipts":
            from native_review_receipt_probe import run_receipt_probe

            receipts = run_receipt_probe()
        else:
            background = background_review_probe()
            stream = stream_observer_probe()
        gc.collect()
    attempts = connect.call_count + connect_ex.call_count
    assert attempts == 0, f"Unexpected network connection attempts: {attempts}"
    after = host_hashes(source)
    assert plugin_before == plugin_hashes(), "Plugin code changed during the audit; rerun against a fixed tree"
    assert before == after, "Audit must never change host sources"
    assert subprocess.run(["git", "-C", str(source), "diff", "--quiet", "HEAD"]).returncode == 0, (
        "Audit must leave all tracked host files unchanged"
    )
    report = {
        "hermes_release": "v0.23.1" if expected_revision == TARGET_REVISION else "candidate",
        "hermes_revision": expected_revision,
        "python": sys.version.split()[0],
        "host_sources_unchanged": before == after, "host_source_sha256": after,
        "plugin_source_sha256": plugin_before,
        "network_calls": attempts,
    }
    if probe == "regression":
        report.update(status="passed" if regression["exit_code"] == 0 else "blocked", regression=regression)
    elif probe == "platform":
        report.update(status="passed" if platform["all_checks_passed"] else "blocked", platform=platform)
    elif probe == "tool-receipts":
        report.update(status="passed" if receipts["all_checks_passed"] else "blocked", tool_receipts=receipts)
    else:
        report.update(status="blocked" if not background["preserved"] else "passed",
                      background_review=background, stream_observers=stream)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--expected-revision", default=TARGET_REVISION, help="Exact immutable host commit to audit")
    parser.add_argument("--probe", choices=("baseline", "tool-receipts", "platform", "regression"), default="baseline")
    parser.add_argument(
        "--fixture-source", type=Path, help="Local Hermes git repository containing pinned legacy fixtures",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    source = args.source.resolve()
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if len(args.expected_revision) != 40 or any(c not in "0123456789abcdef" for c in args.expected_revision):
        parser.error("--expected-revision must be a full lowercase commit SHA")
    if revision != args.expected_revision:
        parser.error(f"Expected pinned Hermes {args.expected_revision}, got {revision}")
    if subprocess.run(["git", "-C", str(source), "diff", "--quiet", "HEAD"]).returncode:
        parser.error("Target Hermes must have no tracked source changes")
    if args.worker:
        worker_root = os.environ.get("HLS_NATIVE_AUDIT_WORKER_ROOT")
        if (
            not worker_root or Path.home().resolve() != Path(worker_root).resolve()
            or Path(os.environ.get("HERMES_HOME", "")).resolve() != Path(worker_root).resolve() / ".hermes"
        ):
            parser.error("Internal worker requires the parent's disposable HOME")
        print(RESULT_PREFIX + json.dumps(worker(source, args.probe, args.expected_revision), ensure_ascii=False))
        return 0
    plugin = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="hls-native-audit-") as directory:
        home = Path(directory)
        # Do not carry model credentials, provider endpoints, PYTHONHOME or
        # session/profile variables from the user's running gateway.
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LANG") if key in os.environ}
        env.update(
            HOME=str(home), HERMES_HOME=str(home / ".hermes"),
            HLS_NATIVE_AUDIT_WORKER_ROOT=str(home),
            HERMES_RUNTIME_DIR=str(home / "runtime"), HERMES_DISABLE_LAZY_INSTALLS="1",
            PYTHONPATH=os.pathsep.join((str(source), str(plugin))),
        )
        if args.probe == "regression":
            if args.fixture_source is None:
                parser.error("--probe regression requires --fixture-source")
            env["HLS_AUDIT_FIXTURE_SOURCE"] = str(args.fixture_source.resolve())
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--source", str(source), "--worker", "--probe", args.probe,
             "--expected-revision", args.expected_revision],
            cwd=source, env=env, capture_output=True, text=True, timeout=600 if args.probe == "regression" else 90,
        )
        if result.returncode:
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps({
                    "status": "error", "hermes_revision": revision, "probe": args.probe,
                    "exit_code": result.returncode, "stderr": result.stderr[-16000:],
                    "stdout": result.stdout[-8000:],
                }, indent=2))
            sys.stderr.write(result.stderr)
            sys.stderr.write(result.stdout)
            return 1
        if args.report and args.probe == "regression":
            args.report.with_suffix(".log").write_text(result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.startswith(RESULT_PREFIX)]
        if len(lines) != 1:
            raise RuntimeError("Worker did not return exactly one audit result")
        report = json.loads(lines[0][len(RESULT_PREFIX):])
        if args.probe == "regression" and report["regression"]["counts"].get("skipped", 0):
            report["status"] = "blocked"
        lifecycle_warnings = [
            line for line in result.stderr.splitlines()
            if "was never awaited" in line or "Task was destroyed but it is pending" in line
        ]
        if lifecycle_warnings:
            raise RuntimeError("Audit introduced coroutine lifecycle warnings: " + "\n".join(lifecycle_warnings))
        report["coroutine_lifecycle_warnings"] = lifecycle_warnings
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.report:
        args.report.write_text(rendered)
    print(rendered, end="")
    return 2 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
