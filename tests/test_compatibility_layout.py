"""Pinned source contracts plus synthetic inheritance/unsupported-layout regressions."""

from __future__ import annotations

import builtins
import inspect
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from hermes_sources import MODULAR_REVISION, TARGET_REVISION, TRANSITION_REVISION, source_at
from test_split_gateway import context, method

from hermes_lark_streaming import __main__ as cli
from hermes_lark_streaming.compatibility import SourceResolver
from hermes_lark_streaming.patcher import (
    CronPatcher,
    FeishuAdapterPatcher,
    Patcher,
    PatcherError,
    _clarify_hook,
    _clean_hooks,
    install_patchers,
)
from hermes_lark_streaming.split_gateway import GATEWAY_FILES, inject_gateway


@pytest.fixture(params=[TARGET_REVISION, TRANSITION_REVISION, MODULAR_REVISION],
                ids=['0.21.3', '0.21.6', '0.21.7'])
def tree(tmp_path, request):
    paths = ['gateway/run.py', *(f'gateway/{name}' for name in GATEWAY_FILES),
             'cron/scheduler.py', 'cron/scheduler_delivery.py', 'plugins/platforms/feishu/adapter.py']
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source_at(relative, request.param))
    return tmp_path


def patchers(root):
    return [Patcher(root / 'gateway/run.py'),
            FeishuAdapterPatcher(root / 'plugins/platforms/feishu/adapter.py'),
            CronPatcher(root / 'cron/scheduler.py')]


def snapshot(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_real_layout_and_complete_transactional_roundtrip(tree):
    targets = patchers(tree)
    gateway, adapter, cron = targets
    assert gateway.layout.kind == 'modular'
    assert {role for role, _path in gateway.layout.modules} == set(GATEWAY_FILES)
    assert cron.split and cron.cron_path.name == 'scheduler_delivery.py'
    before = snapshot(tree)
    plan = {path: text for p in targets for path, text in p.prepare_install().items()}
    assert len(plan) == 7  # entry module + four gateway modules + adapter + cron
    for name in ('NORMALIZE', 'START', 'ANSWER', 'REASONING', 'TOOL', 'PROGRESS',
                 'STATUS', 'BACKGROUND_REVIEW', 'CLARIFY', 'CLARIFY_SEND', 'CLARIFY_ACTION',
                 'COMPLETE', 'ABORT', 'INTERRUPT', 'STOP', 'USAGE', 'USAGE_BASELINE',
                 'BG_DELIVER', 'FOLLOWUP_COMPLETE', 'FOLLOWUP_RESULT', 'APPROVAL_UI',
                 'APPROVAL_ACTION', 'CRON_DELIVER'):
        assert any(f'# HERMES_LARK_{name}_BEGIN' in text for text in plan.values()), name
    assert snapshot(tree) == before
    install_patchers(targets)
    assert all(p.is_fully_patched() for p in patchers(tree))
    installed = snapshot(tree)
    install_patchers(patchers(tree))
    assert snapshot(tree) == installed
    for p in targets:
        p.remove()
    assert all((tree / path).read_bytes() == content for path, content in before.items())
    install_patchers(targets)
    for p in targets:
        p.restore()
    assert all((tree / path).read_bytes() == content for path, content in before.items())
    assert not any(p.is_patched() for p in targets)
    assert adapter.is_fully_patched() is False


def test_discovery_follows_composed_imports_when_module_moves(tree):
    old = tree / 'gateway/run_turn.py'
    new = old.with_name('turns_impl.py')
    old.rename(new)
    entry = tree / 'gateway/run.py'
    entry.write_text(entry.read_text().replace('from gateway.run_turn import', 'from gateway.turns_impl import'))
    gateway = Patcher(entry)
    assert dict(gateway.layout.modules)['run_turn.py'] == new
    gateway.verify_target()
    gateway.apply()
    gateway.remove()
    assert '# HERMES_LARK_' not in new.read_text()


def test_orphan_mixin_with_same_method_is_not_compatibility_evidence(tmp_path):
    (tmp_path / 'run.py').write_text('class Empty:\n    pass\nclass GatewayRunner(Empty):\n    pass\n')
    (tmp_path / 'run_turn.py').write_text('class Orphan:\n    def _handle_message_with_agent(self):\n        pass\n')
    with pytest.raises(PatcherError, match='cannot resolve'):
        Patcher(tmp_path / 'run.py')


def test_unrecognized_cron_cannot_pass_on_unrelated_string_anchors(tmp_path):
    path = tmp_path / 'scheduler.py'
    path.write_text('def unrelated():\n    delivered = False\n    cleaned_delivery_content = "decoy"\n')
    cron = CronPatcher(path)
    before = snapshot(tmp_path)
    with pytest.raises(PatcherError, match='legacy _deliver_result contract'):
        cron.apply()
    assert snapshot(tmp_path) == before


def test_import_alias_and_agent_facade_signature_discovery(tmp_path):
    (tmp_path / 'facade.py').write_text(
        'class Facade:\n    def run_conversation(self, user_message, *, persist_user_timestamp=None):\n        pass\n')
    (tmp_path / 'run_agent.py').write_text(
        'from facade import Facade as TurnBase\nclass AIAgent(TurnBase):\n    pass\n')
    resolver = SourceResolver(tmp_path, Path.read_text)
    method = resolver.require_method(tmp_path / 'run_agent.py', 'AIAgent', 'run_conversation')
    assert method.owner == 'Facade'
    assert method.path == tmp_path / 'facade.py'
    assert method.parameters == ('self', 'user_message', 'persist_user_timestamp')


def test_missing_hook_fails_closed_before_gateway_adapter_or_cron_writes(tree):
    targets = patchers(tree)
    cron = targets[-1]
    cron.cron_path.write_text(cron.cron_path.read_text().replace('coro = _send()', 'coro = other_send()'))
    before = snapshot(tree)
    with pytest.raises(PatcherError, match='seam missing'):
        install_patchers(targets)
    assert snapshot(tree) == before


@pytest.mark.parametrize('operation', ['_cmd_install', '_cmd_verify'])
def test_missing_cron_is_a_required_cli_failure(operation):
    gateway = Mock(split=False)
    adapter = Mock()
    with (patch.object(cli, '_get_patcher', return_value=gateway),
          patch.object(cli, '_get_feishu_patcher', return_value=adapter),
          patch.object(cli, '_get_cron_patcher', return_value=None),
          patch('hermes_lark_streaming.patcher.install_patchers') as install):
        assert getattr(cli, operation)() == 1
    install.assert_not_called()


@pytest.mark.parametrize('batch', [False, True], ids=['legacy', 'questions'])
def test_clarify_wrapper_preserves_signature_results_and_failure_cleanup(monkeypatch, batch):
    entered, exited = Mock(), Mock()
    monkeypatch.setattr('hermes_lark_streaming.patch.on_clarify_enter', entered)
    monkeypatch.setattr('hermes_lark_streaming.patch.on_clarify_exit', exited)
    result = {'answers': {'q1': 'A'}, 'outcome': 'submitted'} if batch else 'A'
    calls = []

    def legacy(question, choices, multi_select=False):
        calls.append((question, choices, multi_select))
        return result

    def modern(questions):
        calls.append(questions)
        if questions == 'fail':
            raise ValueError('cancelled')
        return result

    original = modern if batch else legacy
    agent = SimpleNamespace(clarify_callback=original)
    ctx = SimpleNamespace(event_message_id='message', _status_chat_id='chat', session_key='key')
    exec(_clarify_hook(''), {'agent': agent, 'ctx': ctx})
    assert inspect.signature(agent.clarify_callback) == inspect.signature(original)
    assert agent.clarify_callback.__wrapped__ is original
    if batch:
        questions = [{'qid': 'q1', 'question': 'choose', 'choices': ['A'], 'multi_select': False}]
        assert agent.clarify_callback(questions=questions) is result
        assert calls == [questions]
        with pytest.raises(ValueError, match='cancelled'):
            agent.clarify_callback('fail')
        assert exited.call_count == 2
    else:
        assert agent.clarify_callback('choose', ['A'], multi_select=True) == result
        assert calls == [('choose', ['A'], True)]
    assert entered.call_count == exited.call_count


def test_unknown_clarify_contract_fails_closed(tree):
    path = tree / 'gateway/run_turn_runner.py'
    text = path.read_text().replace('def _clarify_callback_sync(self, questions)',
                                   'def _clarify_callback_sync(self, unknown)')
    text = text.replace('def _clarify_callback_sync(self, question: str, choices, multi_select: bool = False)',
                        'def _clarify_callback_sync(self, unknown)')
    assert text != path.read_text()
    path.write_text(text)
    before = snapshot(tree)
    with pytest.raises(PatcherError, match='unsupported clarify callback'):
        Patcher(tree / 'gateway/run.py')
    assert snapshot(tree) == before


def test_plans_remove_only_our_markers(tree):
    targets = patchers(tree)
    for p in targets:
        for path, generated in p.prepare_install().items():
            assert _clean_hooks(generated, p.MARKERS) == path.read_text()


def test_source_only_contract_does_not_require_hermes_imports(tree):
    original_import = builtins.__import__

    def no_hermes(name, *args, **kwargs):
        if name.partition('.')[0] in {'gateway', 'run_agent', 'agent', 'cron', 'tools'}:
            raise ImportError('Hermes dependencies deliberately unavailable')
        return original_import(name, *args, **kwargs)

    with patch('builtins.__import__', side_effect=no_hermes):
        for p in patchers(tree):
            p.verify_target()


def test_composition_changed_after_detection_cannot_patch_orphan_modules(tree):
    gateway = Patcher(tree / 'gateway/run.py')
    entry = gateway.run_path
    entry.write_text(entry.read_text().replace('from gateway.run_turn import', 'from gateway.missing_turn import'))
    before = snapshot(tree)
    with pytest.raises(PatcherError, match='cannot resolve'):
        gateway.apply()
    assert snapshot(tree) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('revision', [TRANSITION_REVISION, MODULAR_REVISION], ids=['0.21.6', '0.21.7'])
async def test_human_silence_normalization_precedes_card_completion(revision, monkeypatch):
    generated = inject_gateway('run_turn.py', source_at('gateway/run_turn.py', revision))
    completed = AsyncMock(return_value=True)
    aborted = Mock()
    hooks = SimpleNamespace(on_message_completed_wait=completed, on_message_aborted=aborted,
                            on_message_needs_text_fallback=Mock(return_value=False))
    monkeypatch.setitem(sys.modules, 'hermes_lark_streaming.patch', hooks)
    owner = SimpleNamespace(
        _run_agent_stream_confirmed_final_delivery=lambda *a, **k: False,
        _is_intentional_silence=lambda result, text: text == '[SILENT]',
        _deliver_queued_first_response=AsyncMock(return_value=True),
        _pop_post_delivery_callback=lambda *a: None,
    )
    fn = method({'run_turn.py': generated}, 'run_turn.py', '_run_agent_deliver_first_response',
                silence_allowed=lambda *a: False, _unexpected_silence_reply=lambda: 'visible fallback')
    response = {'final_response': '[SILENT]'}
    await fn(owner, context(), object(), response, response, None)
    completed.assert_awaited_once()
    assert completed.await_args.kwargs['answer'] == 'visible fallback'
    aborted.assert_not_called()
    assert response['already_sent'] is True
    assert owner._deliver_queued_first_response.await_args.args == ('visible fallback',)
    assert owner._deliver_queued_first_response.await_args.kwargs['text_already_delivered'] is True


@pytest.mark.parametrize('revision', [TRANSITION_REVISION, MODULAR_REVISION], ids=['0.21.6', '0.21.7'])
@pytest.mark.parametrize('muted', [False, True])
def test_start_honors_upstream_diagnostic_mute(revision, muted, monkeypatch):
    generated = inject_gateway('run_turn.py', source_at('gateway/run_turn.py', revision))
    body = generated.split('# HERMES_LARK_START_BEGIN', 1)[1].split('# HERMES_LARK_START_END', 1)[0]
    started = Mock()
    monkeypatch.setitem(sys.modules, 'hermes_lark_streaming.patch',
                        SimpleNamespace(on_message_started=started))
    exec(textwrap.dedent(body), {
        'source': SimpleNamespace(platform=SimpleNamespace(value='feishu'), chat_id='chat'),
        'event': SimpleNamespace(message_id='inbound'),
        'self': SimpleNamespace(_reply_anchor_for_event=lambda e: 'anchor'),
        'diagnostic_wake_muted': lambda e: muted,
    })
    assert started.call_count == int(not muted)
