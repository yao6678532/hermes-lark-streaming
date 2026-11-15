# AGENTS.md

## Project

Native Hermes Feishu platform plugin, tested against Hermes v0.23.1 commit
`3b421578588243f920886e6fe79a6faa87ea026e`. Preserve the existing streaming
controller, card design, configuration, and platform identity `feishu`.

## Commands

Use the Python of the actual Hermes PM environment, not a historical `venv` path.

```bash
$HERMES_PYTHON -m hermes_lark_streaming verify
$HERMES_PYTHON -m hermes_lark_streaming status
$HERMES_PYTHON -m hermes_lark_streaming migrate-config        # dry run
$HERMES_PYTHON -m hermes_lark_streaming migrate-config --apply
$HERMES_PYTHON -m ruff check .
$HERMES_PYTHON -m mypy hermes_lark_streaming/
$HERMES_PYTHON scripts/verify_native_compatibility.py --source <clean-pinned-host> --probe platform --report <report>
$HERMES_PYTHON scripts/verify_native_compatibility.py --source <clean-pinned-host> --probe regression --fixture-source <old-host-git-repo> --report <report>
```

Installation/removal uses `hermes plugins`; see INSTALL.md. Commit reviewed
local changes before installing from file://, because the manager clones Git.

## Architecture and constraints

- `plugin.yaml` and `register(ctx)` register `FeishuStreamingAdapter` through
  official `register_platform`. Inherit native connection, permissions, pairing,
  group gates and media handling. Never mutate host source or global objects.
- `native_adapter.py` receives official edit transport snapshots; `native_bridge.py`
  maps lifecycle/tool/human-input events onto the existing controller. Authoritative
  body/final snapshots and full post-API reasoning repair asynchronous observer loss.
- Identity includes profile, inbound message, session, run and API iteration;
  message identity and reply anchor differ for quoted messages. Late old events
  must not overwrite or finalize a newer card. API end is not whole-turn end.
- `native_hooks.py` only copies official callback data and captures bound context.
  Background review results use the real write origin and host summarizer.
  `review_receipts.py` journals successful results, `native_reviews.py` merges
  pending old/new receipts into the latest card bottom and ACKs only confirmed
  delivery. Preserve notification settings, proposals, retry and restart recovery.
- Existing `controller.py`, `streaming/`, `cardkit/`, `feishu.py`, configuration,
  Clarify v2, approval presentation, image handling, Run Details/usage/quota remain
  the product implementation. CardKit schema remains 2.0; splitting preserves
  full content and only the last card footer.
- CardKit creation/final-update failure must retain native final delivery exactly
  once, including queued handoffs; media still uses the native sender.
- `legacy_cli.py`, `patcher.py`, split helpers and old AST fixture tests are retained
  solely for marker cleanup, historical regression and rollback. No old AST path
  is registered in the native runtime. Cleanup removes only our marked blocks;
  never restore historical whole-file `.hermes_lark.bak` over host source.
- Verify both registered factory and actual live adapter; detect old injections.
- Tests must not send live Feishu messages or make real model requests. Do not
  skip critical compatibility scenarios. Known baseline warnings are documented
  in docs/hermes-v0.23.1-native-compatibility.md.
- Commit messages: body uses unnumbered `- item` bullets. Do not push or publish
  unless explicitly requested.
