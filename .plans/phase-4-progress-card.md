# Phase 4 — Long-Running Progress Inside the Streaming Card

## 1. Goal

When an active Feishu streaming card exists, stop sending separate long-running status messages such as:

```text
⏳ Working — 3 min — iteration 3/60
```

Instead update a fixed status region inside the existing card:

```text
⏳ 运行 3 min · iteration 3/60
```

The final completed card should not retain stale Working status.

---

## 2. Core Principle

Do not detect Working status by parsing outbound text.

Forbidden approach:

```python
if text.startswith("⏳ Working"):
    ...
```

That is fragile and can intercept real assistant/user content.

Hook the actual Hermes long-running progress / heartbeat generation path.

---

## 3. Configuration

Recommended:

```yaml
streaming:
  progress_mode: card
```

Default:

```text
text
```

Accepted:

```text
text
card
```

Invalid values:

```text
fallback to text
```

This preserves current behavior unless explicitly enabled.

---

## 4. Presentation State

Recommended file:

```text
hermes_lark_streaming/streaming/progress.py
```

Suggested state:

```python
class ProgressState:
    elapsed_seconds: float
    iteration: int | None
    max_iterations: int | None
    text: str
    dirty: bool
    visible: bool
```

Optional fields may be added only when driven by real Hermes progress metadata.

Do not encode state solely in formatted strings if structured data is available.

---

## 5. CardSession Integration

Modify:

```text
hermes_lark_streaming/streaming/session.py
```

Add:

```python
self.progress = ProgressState()
```

Conceptual structure:

```text
CardSession
├── segment_state
├── merged_reasoning
├── progress
├── tool_use
├── footer
└── CardKit state
```

Phase 4 must work even if Phase 1 merged reasoning is disabled.

---

## 6. Fixed Progress Element

Define:

```python
PROGRESS_ELEMENT_ID = "progress_status"
```

Preferred architecture:

- create a fixed low-noise notation element in the streaming card, or
- create it once on first progress event.

Avoid repeated add/remove operations if a fixed element can be updated.

The card element should look like status metadata rather than answer content.

Example:

```text
⏳ 运行 3 min · iteration 3/60
```

English locale:

```text
⏳ Working · 3 min · iteration 3/60
```

Use existing i18n conventions.

---

## 7. Hermes Progress Hook

Locate the current Hermes source where:

```text
long_running_notifications
```

produce the Working heartbeat.

Do not infer from output strings.

Add a semantic patch/hook around the progress notification generation/send path.

Suggested plugin hook:

```python
on_long_running_progress(...)
```

Possible data:

```python
message_id
session_key
chat_id
elapsed_seconds
iteration
max_iterations
raw_status
```

Use only fields actually available in current Hermes.

---

## 8. Hook Return Contract

Recommended behavior:

```text
if not Feishu/Lark:
    return False

if progress_mode != card:
    return False

if no active StreamCard session:
    return False

if active card/session can accept update:
    update ProgressState
    schedule flush
    return True

if any failure before ownership is safe:
    return False
```

`True` means:

```text
the plugin owns this progress update
and Hermes should not emit the separate text message
```

`False` means:

```text
Hermes continues its original text behavior
```

This guarantees fail-open behavior.

---

## 9. Mapping to Active CardSession

Use stable IDs already available in the plugin:

```text
message_id
session_key
anchor/reply mapping
```

Do not guess the active card from chat ID alone when several runs can exist.

Prefer existing controller/session maps.

If no unambiguous active session is found:

```text
return False
```

and keep the original text heartbeat.

---

## 10. Streaming Flush

When ProgressState is dirty:

```text
update/stream progress_status
```

Use the existing CardKit sequence discipline.

On success:

```text
dirty = False
visible = True
```

If another update arrives concurrently, preserve dirty status correctly.

Avoid introducing new sequence races.

---

## 11. Completed Card

The final card must not contain stale:

```text
⏳ Working
```

When the turn completes:

```text
progress.visible = False
```

Terminal builder should omit progress.

The current `yao-custom` footer remains the final status area.

Do not merge long-running progress data into the custom final footer in this phase.

---

## 12. Error and Aborted State

On error/abort:

- do not leave an active-looking Working line if the card can be finalized;
- preserve existing final error/stopped card semantics;
- keep current custom footer behavior.

Do not let progress cleanup break failure fallback.

---

## 13. Clarify Interaction

When Hermes enters a blocking clarify wait, an active:

```text
⏳ Working
```

status may become misleading.

Preferred behavior if a clean hook is available:

```text
progress = "⏸ 等待你的选择"
```

or hide the Working line.

However, this is not required if it couples Phase 4 too tightly to Phase 2.

Minimum requirement:

- do not create additional separate Working messages while clarify is pending and the card still owns progress.

Document the chosen behavior.

---

## 14. Approval Interaction

Same principle as Clarify:

while Hermes is waiting for execution approval, showing continuously increasing Working time may be misleading.

Optional later improvement:

```text
⏸ 等待执行授权
```

Do not add invasive cross-phase state coupling solely for this cosmetic enhancement.

---

## 15. CardKit Failure

If the progress element update fails:

- do not fail the Agent;
- do not corrupt answer/reasoning state;
- if possible allow future Hermes text heartbeat fallback;
- preserve normal final reply.

If the hook has already told Hermes the message was handled, ensure a failed CardKit update does not permanently silence all visibility for a long-running task.

Choose ownership only after it is reasonably safe.

---

## 16. Patcher Marker

Suggested:

```text
HERMES_LARK_PROGRESS_BEGIN
HERMES_LARK_PROGRESS_END
```

Add:

- apply;
- verify;
- unpatch;
- repair;
- compatibility tests.

Use semantic anchors around current Hermes progress notification logic.

---

## 17. Tests

### Config

Default:

```text
progress_mode=text
```

Explicit:

```text
progress_mode=card
```

### No active card

Hook returns:

```text
False
```

Hermes text fallback remains.

### Active card

Hook returns:

```text
True
```

ProgressState updated.

### Multiple updates

Example:

```text
1 min iteration 1
2 min iteration 2
3 min iteration 3
```

Assertions:

- same fixed element ID;
- no repeated structural element creation after first setup;
- latest content wins.

### Completion

Assert:

- progress omitted from terminal card;
- existing footer unchanged.

### Aborted/error

Assert:

- no stale live Working state remains in final representation.

### CardKit update failure

Assert:

- no Agent failure;
- no corrupted session state;
- fallback behavior is preserved.

### Non-progress messages

Normal text containing:

```text
⏳ Working
```

must not be intercepted based on text matching.

### Session mapping

A progress update for one session must not update another active card in the same chat.

---

## 18. Manual Feishu Verification

Run a task exceeding Hermes' long-running notification threshold.

Before Phase 4:

```text
streaming card
⏳ Working text message
streaming card update
⏳ Working text message
```

After Phase 4 with:

```yaml
streaming:
  progress_mode: card
```

verify:

```text
one active streaming card
progress line updates in place
no separate Working messages
```

Then verify completion:

```text
progress line disappears
final answer remains
yao-custom footer remains
```

Also verify:

- `/stop`;
- interrupted turn;
- queued follow-up;
- long tool execution;
- group/thread routing.

---

## 19. Files Likely Involved

Plugin:

```text
hermes_lark_streaming/config.py
hermes_lark_streaming/controller.py
hermes_lark_streaming/streaming/session.py
hermes_lark_streaming/streaming/progress.py
hermes_lark_streaming/streaming/controller.py
hermes_lark_streaming/cardkit/builder.py
hermes_lark_streaming/patch.py
hermes_lark_streaming/patcher.py
tests/*
```

Hermes patch target likely:

```text
gateway/run.py
```

Use the actual current code as truth.

---

## 20. Compatibility Requirements

The new progress patch anchor must be included in upstream compatibility checks.

When Hermes changes the long-running notification implementation:

CI should report a clear compatibility failure.

Do not patch a guessed nearby send call.

---

## 21. Commit Boundary

Recommended:

```text
feat: fold long-running progress into streaming cards
```

Do not expand into unrelated streaming architecture refactors.

---

## 22. Definition of Done

Phase 4 is complete when:

- `progress_mode` exists and defaults to text;
- card mode intercepts the real Hermes long-running progress path;
- no string-prefix parsing is used as the primary detection mechanism;
- an active card shows one fixed progress region;
- repeated Working updates edit/update that region;
- separate Working messages are suppressed only when card ownership succeeds;
- no active card means original text behavior;
- final card removes Working status;
- current `yao-custom` footer is unchanged;
- CardKit failure does not break the Agent;
- session mapping cannot cross-update another run;
- automated tests pass;
- real Feishu long-running task behavior is manually verified.
