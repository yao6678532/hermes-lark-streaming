"""Full-dependency staging check. Always copy source into a disposable temp HOME.

Run with the target Hermes venv Python, e.g.:
    <hermes-venv>/bin/python scripts/verify_hermes_runtime.py --source <checkout>

No import failure is converted into an AST-only success. No adapter connects and
no model request is made; real Feishu delivery remains a manual smoke check.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from hermes_lark_streaming.compatibility import SourceResolver
from hermes_lark_streaming.patcher import (
    CronPatcher,
    FeishuAdapterPatcher,
    Patcher,
    _atomic_write,
    _clean_hooks,
    install_patchers,
)


def probe(root: Path) -> None:
    root = root.resolve()
    # Intentionally uncaught: dependency/import/runtime failures are failures.
    from gateway.config import GatewayConfig, Platform
    from gateway.platform_registry import platform_registry
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_discovery import collect_directory_manifests
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from plugins.platforms.feishu.adapter import FeishuAdapter
    from run_agent import AIAgent
    from tools import clarify_gateway

    import hermes_lark_streaming.patch as hooks
    from hermes_lark_streaming.controller import get_controller

    resolver = SourceResolver(root, lambda p: _clean_hooks(p.read_text(), Patcher.MARKERS))
    for method in ('_handle_message', '_handle_message_with_agent', '_busy_stop_command'):
        location = resolver.require_method(root / 'gateway/run.py', 'GatewayRunner', method)
        actual = getattr(GatewayRunner, method)
        assert Path(inspect.getfile(actual)).resolve() == location.path.resolve()
        assert tuple(inspect.signature(actual).parameters) == location.parameters
    conversation = resolver.require_method(root / 'run_agent.py', 'AIAgent', 'run_conversation')
    assert Path(inspect.getfile(AIAgent.run_conversation)).resolve() == conversation.path.resolve()
    assert tuple(inspect.signature(AIAgent.run_conversation).parameters) == conversation.parameters
    assert {'user_message', 'conversation_history', 'persist_user_timestamp',
            'persist_user_platform_id'} <= set(conversation.parameters)
    assert {'stream_delta_callback', 'reasoning_callback', 'clarify_callback',
            'interim_assistant_callback', 'status_callback'} <= set(inspect.signature(AIAgent).parameters)
    assert callable(FeishuAdapter.send_exec_approval) and callable(FeishuAdapter.send_clarify)
    manager = PluginManager()
    manifests = [m for m in collect_directory_manifests()
                 if m.path and Path(m.path).resolve() == root / 'plugins/platforms/feishu']
    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest.kind == 'platform' and manifest.source == 'bundled'
    manager._load_plugin(manifest)
    loaded = manager._plugins[manifest.key or manifest.name]
    assert loaded.enabled and not loaded.error, loaded.error
    entry = platform_registry.get('feishu')
    assert entry is not None
    runtime_adapter = entry.adapter_factory
    assert runtime_adapter.__module__ == loaded.module.__name__ + '.adapter'
    assert Path(inspect.getfile(runtime_adapter)).resolve() == root / 'plugins/platforms/feishu/adapter.py'
    assert 'HERMES_LARK_APPROVAL_UI' in inspect.getsource(runtime_adapter._send_exec_approval_prompt)
    assert 'HERMES_LARK_APPROVAL_ACTION' in inspect.getsource(runtime_adapter._handle_approval_card_action)
    controllers = []
    for name in ('profile-a', 'profile-b'):
        home = root.parent / name
        home.mkdir()
        (home / 'config.yaml').write_text(
            f'gateway:\n  platforms:\n    feishu:\n      extra:\n        app_id: {name}\n'
            f'        app_secret: staging-{name}\n')
        token = set_hermes_home_override(home)
        try:
            controller = get_controller()
            assert controller._profile_home == home.resolve()
            assert controller._cfg.feishu_app_id == name
            assert controller._cfg.feishu_app_secret == f'staging-{name}'
            controllers.append(controller)
        finally:
            reset_hermes_home_override(token)
    assert controllers[0] is not controllers[1]
    # Constructor initializes state/config/DB only under the disposable HOME.
    gateway = GatewayRunner(GatewayConfig())
    gateway._service_tier = 'normal'

    class Adapter:
        def __init__(self):
            self._post_delivery_callbacks = {}

        def pause_typing_for_chat(self, _chat):
            pass

        def resume_typing_for_chat(self, _chat):
            pass

        async def send_clarify(self, **kwargs):
            assert clarify_gateway.resolve_gateway_clarify(kwargs['clarify_id'], kwargs['choices'][0])
            return SendResult(success=True, message_id='staging-clarify')

    source = SessionSource(platform=Platform.FEISHU, chat_id='staging-chat', user_id='staging-owner')
    ctx = TurnContext(source=source, inbound_message_id='staging-message',
                      event_message_id='staging-anchor', session_key='staging-session',
                      _run_still_current=lambda: True, user_config={},
                      _hooks_ref=NS(loaded_hooks=False), _status_adapter=Adapter(),
                      _status_chat_id='staging-chat')
    turn = TurnRunner(gateway, ctx)
    # Execute scheduled coroutines locally; dummy adapter never calls the SDK.
    def schedule(coro, _label):
        future = concurrent.futures.Future()
        future.set_result(asyncio.run(coro))
        return future

    turn._schedule = schedule
    agent = AIAgent.__new__(AIAgent)
    agent.model = 'staging-model'
    agent.api_mode = 'chat_completions'
    agent.session_id = 'staging-session'
    agent._stream_callback = None
    records = {name: Mock(return_value=True) for name in (
        'on_answer_delta', 'on_thinking_delta', 'on_reasoning_delta', 'on_tool_updated',
        'on_clarify_enter', 'on_clarify_exit', 'on_background_review_message',
    )}
    with patch.multiple(hooks, **records), patch.object(
        hooks, 'get_controller', return_value=NS(enabled=True, _sessions={'staging-message': object()}),
    ):
        consumer, delta, interim, want_interim = turn._setup_stream_consumer('feishu')
        assert consumer is None and want_interim is True
        ctx.progress_callback = turn.progress_callback
        turn._wire_turn_agent_callbacks(agent, {}, {'enabled': True}, delta, interim, want_interim)
        # Actual AIAgent producer -> patched TurnRunner consumer -> plugin hook.
        agent._fire_stream_delta('answer')
        agent._fire_reasoning_delta('reason')
        agent._deliver_interim('commentary', already_streamed=False, record=['commentary'])
        agent._deliver_interim('already visible', already_streamed=True, record=[])
        agent.tool_progress_callback('tool.started', 'terminal', 'preview')
        agent.background_review_callback('background review')
        for name, text in (('on_answer_delta', 'answer'), ('on_reasoning_delta', 'reason'),
                           ('on_thinking_delta', 'commentary')):
            records[name].assert_called_once()
            assert records[name].call_args.kwargs['message_id'] == 'staging-message'
            assert records[name].call_args.kwargs['text'] == text
        records['on_tool_updated'].assert_called_once()
        records['on_background_review_message'].assert_called_once()
        original = turn._clarify_callback_sync
        assert inspect.signature(agent.clarify_callback) == inspect.signature(original)
        if 'questions' in inspect.signature(original).parameters:
            answer = agent.clarify_callback([
                {'qid': 'q1', 'question': 'choose', 'choices': ['A', 'B'], 'multi_select': False},
                {'qid': 'q2', 'question': 'choose again', 'choices': ['C', 'D'], 'multi_select': False},
            ])
            assert answer == {'answers': {'q1': 'A', 'q2': 'C'}, 'outcome': 'submitted'}
        else:
            assert agent.clarify_callback('choose', ['A', 'B']) == 'A'
        records['on_clarify_enter'].assert_called_once()
        records['on_clarify_exit'].assert_called_once()
        assert clarify_gateway.has_pending('staging-session') is False
    print(json.dumps({'imports': 'passed', 'gateway_initialization': 'passed',
                      'runtime_callback_wiring': 'passed', 'feishu_loader': 'passed',
                      'feishu_module': runtime_adapter.__module__,
                      'credential_profiles': 'passed', 'python': sys.version.split()[0]}))


def staging(source: Path) -> None:
    plugin = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix='hls-runtime-') as directory:
        home = Path(directory)
        root = home / 'hermes-agent'
        shutil.copytree(source, root, ignore=shutil.ignore_patterns(
            '.git', 'venv', '.venv', 'node_modules', '__pycache__', 'tests', 'docs', 'website', 'desktop'))
        env = {'HOME': str(home), 'HERMES_HOME': str(home),
               'PATH': str(Path(sys.executable).parent) + os.pathsep + os.defpath,
               'PYTHONPATH': str(root) + os.pathsep + str(plugin), 'TZ': 'UTC'}
        targets = [Patcher(root / 'gateway/run.py'),
                   FeishuAdapterPatcher(root / 'plugins/platforms/feishu/adapter.py'),
                   CronPatcher(root / 'cron/scheduler.py')]
        paths = [path for p in targets for path in p.prepare_install()]
        before = {p: p.read_bytes() for p in paths}

        def cli(command):
            result = subprocess.run([sys.executable, '-m', 'hermes_lark_streaming', command],
                                    env=env, cwd=root, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError(f'{command} failed:\n{result.stdout}\n{result.stderr}')
            print(f'CLI {command}: passed')

        cli('verify')
        assert all(p.read_bytes() == value for p, value in before.items())
        cli('install')
        assert all(p.is_fully_patched() for p in targets)
        installed = {p: p.read_bytes() for p in paths}
        cli('verify')
        cli('install')
        assert all(p.read_bytes() == value for p, value in installed.items())
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--probe', str(root)],
                                env=env, cwd=root, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f'Runtime probe failed:\n{result.stdout}\n{result.stderr}')
        print(result.stdout.strip())
        cli('uninstall')
        assert all(p.read_bytes() == value for p, value in before.items())
        cli('install')
        cli('restore')
        assert all(p.read_bytes() == value for p, value in before.items())
        snapshots = {p: p.read_bytes() for p in root.rglob('*.hermes_lark.bak')}
        snapshots.update(before)
        failed = False

        def fail_once(path, content):
            nonlocal failed
            if path == targets[-1].cron_path and not failed:
                failed = True
                raise OSError('staging rollback fault')
            _atomic_write(path, content)

        with patch('hermes_lark_streaming.patcher._atomic_write', side_effect=fail_once):
            try:
                install_patchers(targets)
            except OSError as exc:
                assert str(exc) == 'staging rollback fault'
            else:
                raise AssertionError('rollback fault was not exercised')
        assert failed and all(p.read_bytes() == value for p, value in snapshots.items())
        print('Transactional failure rollback: passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--probe', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe:
        probe(args.probe)
    elif args.source:
        staging(args.source.resolve())
    else:
        parser.error('--source is required')
