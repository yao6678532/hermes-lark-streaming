# Native streaming stability — 2026-11-15

## Scope

Base plugin: `1129b3aa6eec129d89051bfa5f66e0e66f66305b`.
Hermes remains v0.23.1, `3b421578588243f920886e6fe79a6faa87ea026e`.
No host source changes, AST runtime, global patches, new user settings, dependency
changes, receipt schema changes, tags or releases. Ordinary slash replies remain
cards; official picker/update prompts and custom Clarify v2/approval keep their
existing design and callback namespaces. Existing card builders remain unchanged.

## Implementation

- Native CI uses sibling host/plugin directories, independent HOME/HERMES_HOME,
  Python 3.14 and official PM exports/builds. Both plugin dependencies and the
  pinned host graph are explicit inputs. Historical source fixtures only test
  cleanup/migration/rollback. Daily release candidates resolve to immutable SHAs;
  candidate results only produce logs/reports and never upgrade a machine.
- Startup binds the actual base adapter call signatures and required hooks,
  avoiding gateway-runner imports during registration. Configured-but-unloaded
  native adapters fail verification.
- Physical CardKit IDs share a sequence/lock across foreground and late-status
  credential clients. A logical operation keeps its UUID through retry; conflict
  recovery is bounded and checks ownership on every attempt. Duplicate element
  recovery requires an identical pending insert with an unknown acknowledgement.
- The existing logical card JSON is projected into physical cards only when the
  UTF-8 budget requires it. This leaves controller segments/final reconciliation
  intact, retains original wrappers/styles/images, and puts the atomic terminal
  footer on the last part. Each physical card has its own sequence and identity.
  A streaming projection leaves 2 KiB below the 27,000-byte ceiling for completion.
  Oversized paragraphs/code/tools split without truncating displayed content.
- Necessary active writes renew streaming after 540 seconds; timeout recovery
  reopens at most once per operation. Explicitly closed/terminal/unavailable
  cards do not resume. No periodic renewal message is added.
- Message confirmation retries use the same UUID. Unknown reference delivery
  keeps the known entity and an internal edit handle; final writes retry its
  confirmation. Persistently unknown message/final-write outcomes are reported
  as failures without sending another copy through the native fallback.
  Explicitly failed creation/finalization still uses the existing native fallback.
- Shutdown stops admission/retry scheduling first, seals independent cards in
  parallel with a 5-second plugin budget, and always disconnects the base adapter.
  Deadline guards prevent new plugin requests after that budget. Undelivered
  background receipts remain in the existing journal; old streaming cards do not
  resume across process restarts.
- Host-owned silent/interactive tasks survive GC. Orphans seal before detaching
  latest-card state; locks with owners/waiters are retained. Completed turns and
  stream indexes are removed. Transport tombstones are limited to 2,048/24h,
  unknown edits fail closed, and only known native message IDs use native edits.

## Acceptance and deployment evidence

Reports and execution results are recorded in `docs/stability-20261115/` after
completion. The audit denies network connections and checks host/plugin hashes
before and after running; no live Feishu test messages or model requests are used.
SDK serialization is checked at the mock network boundary, including UUID,
sequence and `settings.config.streaming_mode`.

Machine backup/restore instructions are private because they include credentials,
PM selection and message data. Rollback restores plugin/selection/configuration
and service state while retaining messages and receipts written after cutover;
never overwrite a live database with the pre-upgrade snapshot.
