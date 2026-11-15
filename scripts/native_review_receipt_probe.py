"""Real-host tool/observer probe, called only by the isolated audit worker."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

_PLUGIN_SOURCE = '''
import threading
from hermes_constants import get_hermes_home
from gateway.session_context import get_session_env
from tools.skill_provenance import get_current_write_origin
from hermes_lark_streaming.review_receipts import ReviewReceiptBuffer, observe_native_result

BUFFER = ReviewReceiptBuffer()
SEEN = []
REPLIES = []
BEFORE_REPLY = None

def receive(**payload):
    SEEN.append(dict(origin=get_current_write_origin(), home=str(get_hermes_home()),
                     chat=get_session_env("HERMES_SESSION_CHAT_ID"),
                     thread=threading.get_ident(), name=payload.get("tool_name"),
                     call_id=payload.get("tool_call_id")))
    return observe_native_result(BUFFER, **payload)

def register(ctx):
    ctx.register_hook("post_tool_call", receive)
    ctx.register_middleware("llm_execution", scripted_response)

def scripted_response(**payload):
    if not REPLIES:
        raise SystemExit("Unexpected provider attempt in offline receipt probe")
    response = REPLIES.pop(0)
    if BEFORE_REPLY is not None:
        BEFORE_REPLY(len(REPLIES))
    return response
'''


def run_receipt_probe() -> dict[str, Any]:
    from agent.background_review import summarize_background_review_actions
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext, emit_terminal_post_tool_call
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_cli.plugins import get_plugin_manager
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.memory_tool_store import MemoryStore
    from tools.skill_manager_tool import skill_manage
    from tools.skill_provenance import reset_current_write_origin, set_current_write_origin
    from tools.thread_context import propagate_context_to_thread

    profiles = Path.home() / "receipt-profiles"
    modules: dict[str, Any] = {}
    checks: dict[str, bool] = {}
    results: dict[str, Any] = {}

    @contextmanager
    def scope(name: str, chat: str = "same-chat"):
        home = profiles / name
        home.mkdir(parents=True, exist_ok=True)
        token = set_hermes_home_override(home)
        session_tokens = set_session_vars(
            platform="feishu", chat_id=chat, session_key=f"session-{name}-{chat}",
            session_id=f"physical-{name}", message_id="old-inbound", profile=name,
        )
        try:
            yield home
        finally:
            clear_session_vars(session_tokens)
            reset_hermes_home_override(token)

    def load_plugin(name: str) -> Any:
        with scope(name) as home:
            directory = home / "plugins/audit-review-receipts"
            directory.mkdir(parents=True)
            (directory / "__init__.py").write_text(_PLUGIN_SOURCE)
            (directory / "plugin.yaml").write_text(
                'name: audit-review-receipts\nversion: "0.0.1"\nkind: standalone\n'
                'provides_hooks: [post_tool_call]\n')
            (home / "config.yaml").write_text(
                "plugins:\n  enabled: [audit-review-receipts]\n  load_timeout_seconds: 0\n"
                "  hook_callback_timeout: 5\n")
            manager = get_plugin_manager()
            manager.discover_and_load()
            loaded = manager._plugins.get("audit-review-receipts")
            assert loaded is not None and loaded.enabled and not loaded.error, getattr(loaded, "error", None)
            assert loaded.module is not None
            return loaded.module

    for name in ("a", "b"):
        modules[name] = load_plugin(name)
    checks["real_plugin_discovery_and_registration"] = True

    def agent_for(name: str, turn: str) -> SimpleNamespace:
        store = MemoryStore(memory_char_limit=4000, user_char_limit=2000)
        store.load_from_disk()
        return SimpleNamespace(
            session_id=f"physical-{name}", _current_turn_id=turn, _current_api_request_id="audit-api",
            _inherited_conversation_id=f"root-conversation-{name}",
            _memory_store=store, _memory_manager=None, _memory_write_origin="background_review",
        )

    def execute(agent: Any, name: str, args: dict[str, Any], call_id: str, *, origin: str = "background_review") -> Any:
        origin_token = set_current_write_origin(origin)
        try:
            if name == "memory":
                raw = INLINE_TOOL_EXECUTORS["memory"](agent, args, InlineToolContext("review-task", call_id))
            elif name == "skill_manage":
                raw = skill_manage(**args)
            else:
                raise AssertionError(name)
            # This is the same terminal producer called by both real executor
            # paths. Neither lifecycle nor plugin dispatch is mocked.
            emit_terminal_post_tool_call(
                agent, function_name=name, function_args=args, result=raw,
                effective_task_id="review-task", tool_call_id=call_id,
            )
            return json.loads(raw)
        finally:
            reset_current_write_origin(origin_token)

    thread_errors: list[BaseException] = []
    release = threading.Event()
    threads = []
    agents = {}
    for name in ("a", "b"):
        with scope(name):
            agent = agent_for(name, "review-old")
            agents[name] = agent

            def work(name=name, agent=agent):
                try:
                    assert release.wait(5)
                    result = execute(agent, "memory", {
                        "action": "add", "target": "memory", "content": f"confirmed audit fact {name}",
                    }, "same-call-id")
                    assert result["success"] is True
                    results[name] = result
                except BaseException as error:
                    thread_errors.append(error)

            thread = threading.Thread(target=propagate_context_to_thread(work), name=f"audit-review-{name}")
            thread.start()
            threads.append(thread)
    release.set()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    if thread_errors:
        raise thread_errors[0]
    a, b = modules["a"], modules["b"]
    home_a, home_b = profiles / "a", profiles / "b"
    batch_a = a.BUFFER.snapshot(home_a, "same-chat")
    batch_b = b.BUFFER.snapshot(home_b, "same-chat")
    assert len(batch_a.receipts) == len(batch_b.receipts) == 1
    assert batch_a.receipts[0].conversation_id == "root-conversation-a"
    assert batch_b.receipts[0].conversation_id == "root-conversation-b"
    assert a.SEEN[0]["home"] == str(home_a) and b.SEEN[0]["home"] == str(home_b)
    assert a.SEEN[0]["origin"] == b.SEEN[0]["origin"] == "background_review"
    assert all(record["thread"] != threading.get_ident() for record in a.SEEN + b.SEEN)
    checks["profile_route_and_origin_survive_real_hook_worker_threads"] = True
    checks["same_chat_call_ids_do_not_cross_profiles"] = True

    with scope("a"):
        # Actual rejected memory mutation: never becomes a confirmed receipt.
        bad = execute(agents["a"], "memory", {"action": "add", "content": ""}, "failed-call")
        assert bad["success"] is False
        assert len(a.BUFFER.snapshot(home_a, "same-chat").receipts) == 1
        checks["failed_actual_write_not_reported_as_updated"] = True

        cancellation_origin = set_current_write_origin("background_review")
        try:
            emit_terminal_post_tool_call(
                agents["a"], function_name="memory", function_args={"action": "add", "target": "memory"},
                result=json.dumps({"error": "Tool execution cancelled", "status": "cancelled"}),
                effective_task_id="review-task", tool_call_id="cancelled-call", status="cancelled",
            )
        finally:
            reset_current_write_origin(cancellation_origin)
        assert len(a.BUFFER.snapshot(home_a, "same-chat").receipts) == 1
        checks["official_cancelled_terminal_event_does_not_claim_update"] = True

        # Same actual tool, but foreground origin: not a self-improvement fact.
        foreground = execute(agents["a"], "memory", {
            "action": "add", "target": "memory", "content": "user-directed foreground fact",
        }, "foreground-call", origin="assistant_tool")
        assert foreground["success"] is True, foreground
        assert len(a.BUFFER.snapshot(home_a, "same-chat").receipts) == 1
        checks["foreground_success_not_mislabeled_background_review"] = True

        before = (home_a / "memories/MEMORY.md").read_text()
        pending = execute(agents["a"], "memory", {
            "action": "patch", "target": "memory",
            "old_text": "confirmed audit fact a", "content": "replacement not yet approved",
        }, "pending-call")
        assert pending["staged"] is True
        assert (home_a / "memories/MEMORY.md").read_text() == before
        pending_batch = a.BUFFER.snapshot(home_a, "same-chat")
        pending_only = type(pending_batch)(tuple(r for r in pending_batch.receipts if r.tool_call_id == "pending-call"))
        assert pending_only.summarize(summarize_background_review_actions) == [pending["message"]]
        checks["pending_approval_keeps_host_proposal_text_and_does_not_claim_applied"] = True
        a.BUFFER.acknowledge(pending_only)

        # A new review's actual skill change is merged with the old pending
        # memory receipt, without waiting for the old native summary callback.
        agents["a"]._current_turn_id = "review-new"
        skill_content = (
            "---\nname: audit-review-skill\ndescription: Temporary offline receipt probe\n---\n"
            "# Audit skill\n\nUse only inside the disposable receipt audit.\n"
        )
        skill = execute(agents["a"], "skill_manage", {
            "action": "create", "name": "audit-review-skill", "content": skill_content,
        }, "skill-call")
        assert skill["success"] is True, skill
        combined = a.BUFFER.snapshot(home_a, "same-chat")
        assert len(combined.receipts) == 2
        lines = combined.summarize(summarize_background_review_actions)
        assert len(lines) == 2
        expected_messages = []
        for index, receipt in enumerate(combined.receipts):
            ident = f"original-{index}"
            expected_messages.extend((
                {"role": "assistant", "tool_calls": [{"id": ident, "function": {
                    "name": receipt.tool_name, "arguments": receipt.args_json,
                }}]},
                {"role": "tool", "tool_call_id": ident, "content": receipt.result_json},
            ))
        assert lines == summarize_background_review_actions(expected_messages, [], "on")
        assert combined.summarize(summarize_background_review_actions, "off") == []
        assert combined.summarize(summarize_background_review_actions, "verbose")
        checks["old_memory_and_new_skill_merge_using_exact_host_summarizer"] = True
        checks["off_and_verbose_notification_modes_follow_host"] = True
        checks.update(asyncio.run(card_update_probe(combined, home_a)))
        a.BUFFER.acknowledge(batch_a)
        assert len(a.BUFFER.snapshot(home_a, "same-chat").receipts) == 1
        checks["old_batch_ack_preserves_new_receipt"] = True
        assert len(b.BUFFER.snapshot(home_b, "same-chat").receipts) == 1
        checks["acknowledgment_is_profile_scoped"] = True

    with scope("a", "other-chat"):
        result = execute(agents["a"], "memory", {
            "action": "add", "target": "user", "content": "separate chat audit profile fact",
        }, "other-chat-call")
        assert result["success"] is True
        assert len(a.BUFFER.snapshot(home_a, "other-chat").receipts) == 1
        assert len(a.BUFFER.snapshot(home_a, "same-chat").receipts) == 1
    checks["same_profile_chats_keep_separate_pending_receipts"] = True

    assert get_hermes_home().resolve() != home_a.resolve()
    loop_module = load_plugin("loop")
    with scope("loop") as loop_home:
        full_loop = full_review_loop_probe(loop_module, loop_home)
    checks.update(full_loop["checks"])
    return {
        "checks": checks, "check_count": len(checks), "all_checks_passed": all(checks.values()),
        "merged_summary": lines,
        "full_review_loop": full_loop,
        "requires_read_only_host_helpers": [
            "tools.skill_provenance.get_current_write_origin",
            "agent.background_review.summarize_background_review_actions",
        ],
        "limits": [
            "Provider responses were scripted through official LLM middleware; no real model request was made.",
            "The observer is not activated in production and the receipt buffer is not durable.",
            "Card updates use the real existing controller with SDK calls mocked; no live Feishu message was sent.",
        ],
    }


async def card_update_probe(batch: Any, home: Path) -> dict[str, bool]:
    """Feed confirmed receipts through the unchanged final-card update path."""
    from agent.background_review import summarize_background_review_actions
    from agent.i18n import t

    from hermes_lark_streaming.agent_status import AgentStatusEvent, conversation_key
    from hermes_lark_streaming.cardkit.builder import AGENT_STATUS_ELEMENT_ID, build_complete_card
    from hermes_lark_streaming.controller import StreamCardController
    from hermes_lark_streaming.streaming.session import CardSession, SessionState

    ctrl = StreamCardController(home)
    ctrl._initialized = True
    ctrl._client = SimpleNamespace(
        cardkit_batch_update=AsyncMock(), cardkit_update=AsyncMock(), send_text_to_chat=AsyncMock(),
    )
    text = t(
        "display.review.summary_callback", summary=" · ".join(batch.summarize(summarize_background_review_actions)),
    )
    event = AgentStatusEvent.background_review(conversation_key=conversation_key("same-chat"), text=text)

    def seal(message: str, card: str, created: float) -> None:
        session = CardSession(message, "same-chat", asyncio.get_running_loop())
        session.created_at = created
        session.set_card(card_id=card, card_msg_id=f"message-{card}")
        session.state = SessionState.STREAMING
        ctrl._sessions[message] = session
        ctrl._register_latest_card(session)
        session.segment_state.on_answer_delta("Existing answer stays intact.")
        card_json = build_complete_card(segments=session.active_segments(), all_tool_steps=[])
        session.state = SessionState.COMPLETED
        ctrl._remember_finalized_card(session, card_json)
        ctrl._cleanup_session(session)

    seal("old-inbound", "old-final-card", 1.0)
    await ctrl._deliver_agent_status(event=event, chat_id="same-chat")
    assert ctrl._client.cardkit_batch_update.await_args.args[0] == "old-final-card"
    seal("new-inbound", "new-final-card", 2.0)
    await ctrl._deliver_agent_status(event=event, chat_id="same-chat")
    assert ctrl._client.cardkit_batch_update.await_args.args[0] == "new-final-card"
    await ctrl._deliver_agent_status(event=event, chat_id="same-chat")
    ref = ctrl._agent_status_manager.registry.get(conversation_key("same-chat"))
    assert ref.agent_status == text
    assert str(ref.card_snapshot).count(AGENT_STATUS_ELEMENT_ID) == 1
    assert "Existing answer stays intact." in str(ref.card_snapshot)
    ctrl._client.send_text_to_chat.assert_not_awaited()
    return {
        "receipt_summary_updates_already_finalized_card": True,
        "late_receipt_targets_latest_card_and_preserves_answer": True,
        "repeated_receipt_update_keeps_one_bottom_status_without_extra_text": True,
    }


def full_review_loop_probe(module: Any, home: Path) -> dict[str, Any]:
    """Actual fork construction, tool loop, origin binding and final summary gate."""
    from agent.background_review import _run_review_in_thread, summarize_background_review_actions
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent

    call = {"id": "full-loop-memory-call", "type": "function", "function": {
        "name": "memory", "arguments": json.dumps({
            "action": "add", "target": "memory", "content": "full review loop confirmed fact",
        }),
    }}
    for message, finish in (
        ({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"),
        ({"role": "assistant", "content": "Review complete."}, "stop"),
    ):
        module.REPLIES.append(ChatCompletion.model_validate({
            "id": "audit-local-completion", "object": "chat.completion", "created": 0,
            "model": "gpt-4.1-mini", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
        }))
    parent = AIAgent(
        model="gpt-4.1-mini", provider="openai", api_mode="chat_completions",
        base_url="https://offline.invalid/v1", api_key="test",
        enabled_toolsets=["memory"], max_iterations=3, quiet_mode=True,
        skip_context_files=True, skip_background_review=True,
        session_id="full-review-parent", platform="feishu",
    )
    native_sends: list[str] = []

    class Adapter:
        async def send(self, _chat: str, text: str, metadata: Any = None) -> SendResult:
            native_sends.append(text)
            return SendResult(success=True, message_id="unused-native-id")

    runner = GatewayRunner(GatewayConfig())
    key = "full-review-generation"
    runner._session_state(key).persistent.run_generation = 1
    ctx = TurnContext(
        source=SessionSource(platform=Platform.FEISHU, chat_id="same-chat", user_id="audit-owner"),
        session_key=key, _status_adapter=Adapter(), _status_chat_id="same-chat",
        _run_still_current=runner._run_still_current_fn(key, 1),
    )
    turn = TurnRunner(runner, ctx)
    parent.background_review_callback, release = turn._make_bg_review_callbacks()
    release()

    def before_reply(remaining: int) -> None:
        if remaining == 0:
            runner._session_state(key).persistent.run_generation = 2

    module.BEFORE_REPLY = before_reply
    _run_review_in_thread(parent, [], "Perform one memory update and finish.", task_cfg={}, review_memory=True)
    assert not module.REPLIES, "Actual review did not consume both scripted responses"
    batch = module.BUFFER.snapshot(home, "same-chat")
    assert len(batch.receipts) == 1, "Actual review's memory tool did not reach the official observer"
    assert "full review loop confirmed fact" in (home / "memories/MEMORY.md").read_text()
    assert not native_sends, "Native summary should be rejected after the generation changed"
    assert batch.summarize(summarize_background_review_actions) == ["Memory updated"]
    parent.release_clients()
    return {"checks": {
        "actual_background_review_fork_and_tool_loop_produce_receipt": True,
        "actual_origin_binding_not_injected_by_probe": True,
        "receipt_survives_generation_change_that_drops_native_summary": True,
    }, "native_summary_deliveries": len(native_sends), "confirmed_receipts": len(batch.receipts)}
