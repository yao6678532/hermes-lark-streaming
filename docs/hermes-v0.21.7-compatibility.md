# Hermes v0.21.7 compatibility audit

Audit date: 2026-10-13. Source contracts and runtime checks are separate claims.
Production Hermes was never patched, installed into, restarted, or reconfigured.

## Stable baseline and source provenance

- [300313 retry PR #15](https://github.com/yao6678532/hermes-lark-streaming/pull/15): merged using a merge commit after local checks and GitHub Tests passed.
- Merge/main commit: `03c1953cdc3c0400684d104eef579f39f8acc0c1`.
- Annotated tag: `hls-hermes-v0.21.3-stable`; tag object `c81fa8efb71f734de6b2691bd79f08aa84740888`; peeled commit equals the merge commit.
- Compatibility branch starts from this exact main baseline, including Clarify v2, Agent Status, first-visible flush, and 300313 retry.

| Hermes release | Pinned source commit | Role |
|---|---|---|
| `v2026.9.14` / 0.21.3 | `345cd2b057a452236de401d3534b8502a7465e8d` | Required stable baseline |
| `v0.21.6` | `818c13be1dc4fd28987e1e881a9408224afd4535` | Source/lifecycle comparison |
| `v0.21.7` | `71d34bad1045610252eda02ee79549436896e784` | Required target |

All three releases already compose GatewayRunner from Mixins. The old monolithic
injector remains supported and tested against the existing legacy / 0.20 / 0.21.0
fixtures; 0.21.3 is the existing split-layout baseline, not a monolithic release.
0.21.6 and 0.21.7 have the same relevant lifecycle structure; 0.21.7 also contains
annotation modernization, which does not change these callback contracts.

## Compatibility inventory

Module abbreviations: inbound = `gateway/run_inbound.py`; turn =
`gateway/run_turn.py`; worker = `gateway/run_turn_runner.py`; busy =
`gateway/run_busy.py`; adapter = `plugins/platforms/feishu/adapter.py`.
Each installed plugin hook is required in the selected layout. Native diagnostic
mute support is an optional capability of the old layout and present in the new
layout. Any missing/ambiguous required hook prevents the entire install.

| Lifecycle / hook | 0.21.3 location and contract | 0.21.6 / 0.21.7 location and semantic difference |
|---|---|---|
| NORMALIZE | inbound `_hm_admit_event`, both `source = event.source` sites | Same identity contract; normalize before reply-anchor selection |
| START | turn `_handle_message_with_agent`, after `_heartbeat_execution_started` | Same admission boundary; honor upstream diagnostic mute before reserving a card |
| ANSWER / interim THINKING | worker `_setup_stream_consumer` | Native streaming configuration evolved; plugin keeps one transport, raw inbound identity, TTS tee, and `already_streamed` guard |
| REASONING | worker `_wire_turn_agent_callbacks`, after reasoning configuration | Same callback; native reasoning emitter remains reachable via AIAgent's StreamDeliveryMixin |
| TOOL | worker `progress_callback`, after native live status/log, before queue gating | Same started/completed contract; no duplicate native tool progress |
| PROGRESS / paused heartbeat | turn `_run_agent_notify_long_running` | Same heartbeat boundary; existing CLARIFY_PAUSED heartbeat path remains active |
| STATUS | worker `_status_callback_sync`, after prepared-message gate | Upstream adds warning/mute policy; observer retains existing processing-status filtering |
| BACKGROUND_REVIEW | worker `_wire_turn_agent_callbacks` / `_make_bg_review_callbacks` | Same deferred release and explicit owning profile; Agent Status receives review text |
| CLARIFY enter/exit | worker `_clarify_callback_sync(question, choices, multi_select)` | Now `_clarify_callback_sync(questions) -> dict`, multiple questions; signature-preserving wrapper forwards arguments/results unchanged |
| CLARIFY_SEND / ACTION | turn `_run_agent_bind_turn_wiring`; inbound `_handle_message` | Same adapter proxy and official registry bridge; new SKIPPED/CANCELLED sentinels retire cards without rendering control bytes |
| APPROVAL_UI / ACTION | adapter `_send_exec_approval_prompt` / `_handle_approval_card_action` | Native copy and timeout bookkeeping evolved; existing presentation/action seams unchanged |
| COMPLETE / final fallback | turn `_handle_message_with_agent`, before `_hmwa_deliver_turn_response` | Same post-persistence boundary, structured usage/footer reconciliation, failed-card native fallback |
| FOLLOWUP_COMPLETE | turn `_run_agent_deliver_first_response` | New human-silence normalization reassigns `_already_streamed`; finalize after normalization, before `_deliver_media` / native delivery |
| FOLLOWUP_RESULT / INTERRUPT | turn `_run_agent_queued_followup` | Same recursive raw inbound ID vs reply anchor, deepest completion ID, media preservation; native delivery ledger remains upstream-owned |
| ABORT / cancellation / STOP | inbound outer finally; turn handler/child cleanup; busy `_busy_stop_command` | Same idempotent cleanup boundaries, including failures before child cleanup is armed |
| BG_DELIVER | turn `_run_background_task_inner`, after `extract_images` | Same reply anchor and fallback/media continuation |
| USAGE_BASELINE / USAGE | worker `run_sync`, around callback wiring/result | Same cumulative canonical counters and current-turn differences; run-details display unchanged |
| CRON_DELIVER | `cron/scheduler_delivery.py`, `_live_send_text` / `_standalone_send` | `_send` becomes async and awaits bounded `_send_to_platform`; validate both AST shapes and retain native timeouts/media/error accounting |
| Feishu loader identity | `hermes_plugins.feishu_platform.adapter` | Actual bundled manifest discovery now loads `hermes_plugins.platforms__feishu.adapter`; physical source hooks survive this namespace change |
| Split / continuation | existing streaming controller and session generation | No runtime policy change; regression suite covers rollover and ownership |
| First-visible / 300313 retry | existing flush/controller code | No policy change; full existing race/retry/rendered-eligibility tests remain required |
| Profile credentials | Hermes home/secret ContextVars; profile-scoped controllers | New Hermes still propagates context into turn workers and SDK executors; runtime probe verifies separate controller/config credential scopes |

AIAgent's `run_conversation` lives in `agent/turn_facade.py` via TurnFacadeMixin
in both baselines. New optional `title_user_message` and `prelude` arguments do
not require wrapping the method. Gateway's actual kwargs/signature checks and
producer-to-consumer wiring are tested; source-only existence is insufficient.

## Resolver and changes

`compatibility.py` follows the composed class's declared local imports and
method ownership, then the actual TurnRunner import/construction in the context
builder. Injector roles map to the resolved paths, so a module rename can be
handled without replacing every filename anchor. Unrelated classes, ambiguous
inheritance, unknown clarify signatures and consolidated role modules fail
closed. Each injector continues to validate semantic AST seams and compile its
generated source. No version threshold selects the implementation.

Cron discovery follows the imported `_deliver_result` symbol, rather than
assuming a sibling filename. Legacy cron also requires the delivery assignment
and cleaned content in the actual `_deliver_result` scope; unrelated matching
strings cannot authorize an install. CLI install and verify now treat missing cron as a
failure. Gateway, Feishu adapter and cron still share one preflight/apply
transaction; backup refresh, rollback, idempotence, uninstall and restore are
preserved. Recovering a damaged legacy file still uses the legacy cleanup path;
it cannot be installed without passing the legacy required contracts.

Runtime changes are limited to compatibility glue: clarify forwarding/terminal
sentinels, first-response completion position and respecting upstream muted
diagnostic turns. CardKit layout, visual theme, Clarify v2 controls, Agent Status,
ConversationIdentity, monitor, footer and retry/flush policies are unchanged.

## Reference projects

- [Aowen v1.8.4](https://github.com/Aowen-Nowor/hermes-lark-streaming/commit/4e5f3b5240d8686182166c732de3a9f9866d6afb): its release fixes integration sentinels by scanning migrated Mixin methods and keeping import-based vs AST-based checks separate. It explicitly reports zero runtime changes. This informed the verification split, not a claim that its implementation supports 0.21.7. Our resolver follows actual reachable owners instead of taking an unrestricted union of candidate classes.
- [aiduPOP current source](https://github.com/monkey2jack/aiduPOP/tree/5b48282620c814eeeb37cf059a0fc318bd6d3621), latest commit 2026-10-10: HermesCompat separates module/capability discovery, recognizes the loader's `hermes_plugins.feishu_platform.adapter` runtime identity, and direct GatewayRunner/AIAgent wrappers use `functools.wraps` so `inspect.signature` sees the true method. This informed signature-preserving clarify forwarding and the runtime-owner checks. Our transactional source patching needs no new Hermes method monkey patches or loader replacement.

Neither reference patching implementation was copied. Runtime architecture and
source transactions remain this plugin's own.
The current aiduPOP reference's alias candidates are not proof of 0.21.7 loader
compatibility: actual bundled discovery now uses the category-derived
`platforms__feishu` namespace. The runtime probe discovers the real manifest,
loads it through Hermes, and checks the registered class's physical source and
installed hooks instead of constructing an old alias.

## Python, dependencies and reproducibility

0.21.3 declares `>=3.11,<3.14` and installs its pinned dependencies on Python
3.11. 0.21.7 declares `>=3.11,<3.15` for the updater transition, but
`.python-version` is 3.14 and most actual dependencies have
`python_version >= '3.14'` markers. A clean 3.11 install of 0.21.7 cannot be
treated as a complete runtime install. The 0.21.7 source metadata version is
`0.0.0`; use the pinned release commit, not package metadata, for provenance.

Create an isolated target Hermes venv, install its actual declared dependencies
with the Feishu extra plus this plugin's dev dependencies, then run:

```bash
<target-hermes-venv>/bin/python scripts/verify_hermes_runtime.py --source <target-hermes-checkout>
```

The script copies source into a disposable HOME, sanitizes child environments,
and exercises CLI verify/install/verify/reinstall, fresh full imports, gateway
initialization, actual AIAgent -> TurnRunner -> plugin callbacks, both clarify
signatures, the loader's actual Feishu adapter identity and installed hooks,
profile credential scopes, uninstall, restore and transactional
failure rollback. No import exception falls back to a success sentinel. It
never connects an adapter or makes a model request.

Source fixtures pin all three releases. CI has a source/unit job and separate
full-dependency runtime jobs for 0.21.3/Python 3.11 and 0.21.7/Python 3.14.
Installation failure fails the runtime job; it is never treated as AST success.

## Validation results

| Check | 0.21.3 | 0.21.6 | 0.21.7 |
|---|---|---|---|
| Source/AST contracts and full hook plan | Passed | Passed | Passed |
| Pinned source lifecycle, delivery, identity and cron matrix | Passed | Passed | Passed |
| Full declared dependency environment | Python 3.11.15, passed | Comparison only; not claimed | Python 3.14.5, passed |
| Full plugin suite in that environment | **1506 passed**, no skips | Covered by pinned source cases | **1506 passed**, no skips |
| Fresh runtime imports, Gateway initialization, signatures and actual producer/consumer wiring | Passed | Not claimed | Passed |
| Native manifest discovery / loader / installed Feishu hooks | Passed, `feishu_platform` | Not claimed | Passed, `platforms__feishu` |
| Two profile credential scopes | Passed | Not claimed | Passed |
| CLI verify/install/verify/reinstall/uninstall/restore and fault rollback | Passed | Source lifecycle passed | Passed |

Final lint/type/diff checks passed: `ruff check .`, `mypy hermes_lark_streaming/`
(37 modules), and `git diff --check`. The supplemental compatibility integration
suite passed 341 cases; the final legacy/layout suite passed 126 cases, including
the tightened unknown-cron check. Both full suites ran with `TZ=UTC` and
`CI=true`, so unavailable required pinned sources could not become skipped tests.

GitHub's new source/unit and two runtime jobs run on the compatibility branch;
their result is reported with the final branch commit in the chat. Phase 2 is
not merged and no compatibility PR is created.

## Manual Feishu smoke still required

No live Feishu E2E result is claimed. On a separately configured 0.21.7 staging
profile, verify:

- Quoted/topic inbound delivery, answer/reasoning/tool order and final media.
- Background review -> Agent Status and background task reply routing.
- Clarify single/multiple questions, multi-select, Other/direct input, paused heartbeat, skip, timeout, cancellation, and resumed output.
- Approval approve/deny/timeout and owner validation.
- Queue chains, nested interrupts, `/stop`, failed turns and gateway shutdown.
- First-visible flush, 300313 retry under real propagation delay, and split/continuation.
- Cron flat chat vs topic/native fallback, media and two independent credential profiles.

Known limitation: real upstream layout changes still require a new semantic
injector contract when lifecycle semantics change or roles consolidate. The
resolver makes discovery reusable; it does not assume future Hermes versions
are compatible. Existing forced callback-failure tests emit an off-loop flush
coroutine warning; this predates the compatibility change and is not hidden.

## Changed files

- Layout/glue: `hermes_lark_streaming/compatibility.py`, `patcher.py`, `split_gateway.py`, `split_cron.py`, `__main__.py`, `interactions/clarify.py`.
- Fixtures/tests: `tests/hermes_sources.py`, `test_compatibility_layout.py`, `test_multifile_patcher.py`, `test_split_gateway.py`, `test_split_gateway_delivery.py`, `test_split_gateway_identity.py`, `test_split_cron.py`, `test_clarify.py`.
- Runtime validation: `scripts/verify_hermes_runtime.py`.
- CI: `.github/workflows/test.yml`, `.github/workflows/hermes-check.yml`.
- Audit/report: this document.
