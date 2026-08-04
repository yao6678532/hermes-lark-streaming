# Phase 2 — Feishu Clarify Choice Card

## 1. Goal

Render Hermes clarify choice prompts as native interactive Feishu cards.

Current fallback experience:

```text
❓ 你希望如何管理这个 Fork？

1. 保留 main 跟踪官方
2. 合并定制版本到 main
3. 将定制分支设为默认分支

Reply with the number, the option text, or your own answer
```

Target V1:

```text
┌─────────────────────────────
│ ❓ 需要你的选择       待回答
│
│ 你希望如何管理这个 Fork？
│
│ [保留 main 跟踪官方]
│ [合并定制版本到 main]
│ [将定制分支设为默认分支]
│
│ [其他回答…]
└─────────────────────────────
```

Clicking an option must resolve the **existing Hermes clarify wait**.

It must not create a new normal user turn.

---

## 2. Hermes Backend Is the Source of Truth

Do not create another clarify runtime.

Reuse Hermes:

```text
tools.clarify_gateway
```

Especially:

```python
resolve_gateway_clarify(clarify_id, response)
mark_awaiting_text(clarify_id)
```

Hermes remains responsible for:

- `clarify_id`;
- pending registry;
- session binding;
- blocked agent thread;
- timeout;
- text fallback;
- text coercion;
- multi-select parsing;
- cleanup;
- resume.

The plugin adds only:

```text
Feishu presentation
+
card-action bridge
```

---

## 3. Scope

V1 includes:

- config `clarify_style`;
- single-select choices;
- `Other` button;
- callback bridge;
- card answered state;
- fallback to current numbered text;
- identity/chat validation;
- idempotence;
- tests.

V1 does not require:

- multi-select form;
- open-ended text input form;
- replacing Hermes clarify backend;
- synthetic normal user messages.

V2 may add CardKit forms after V1 is stable.

---

## 4. Configuration

Recommended:

```yaml
display:
  platforms:
    feishu:
      clarify_style: card
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

Invalid:

```text
fallback to text
```

Use the same per-platform reload pattern as other Feishu display settings where practical.

---

## 5. New Modules

Recommended:

```text
hermes_lark_streaming/interactions/
    __init__.py
    clarify.py
    registry.py
```

and:

```text
hermes_lark_streaming/cardkit/interaction_builder.py
```

Do not place clarify state into:

```text
SegmentState
```

Clarify is not part of the streaming content timeline.

---

## 6. ClarifyCardState / Registry

The Hermes pending registry is the business source of truth.

Plugin registry is only presentation routing state.

Suggested fields:

```python
clarify_id
card_id
card_msg_id
chat_id
session_key
owner_user_id
question
choices
status
created_at
```

Possible status:

```text
pending
answered
awaiting_text
expired
failed
```

Registry lookup:

```text
clarify_id → presentation state
```

Do not use the plugin registry as authority for whether the Hermes wait still exists.

---

## 7. Clarify Card Builder

Pending single-select card:

```text
header:
  template: blue
  title: ❓ 需要你的选择

status:
  待回答
```

Question content should support normal Feishu markdown/plain-text handling.

Each canonical choice becomes a button.

Suggested button value:

```json
{
  "hermes_lark_action": "clarify_select",
  "clarify_id": "<id>",
  "response": "<canonical option text>"
}
```

Other:

```json
{
  "hermes_lark_action": "clarify_other",
  "clarify_id": "<id>"
}
```

Use canonical option text, not only an integer index.

This keeps the callback directly aligned with Hermes' expected response semantics.

---

## 8. Do Not Reuse Hermes Approval Namespace

Do not put clarify under:

```json
{"hermes_action": "..."}
```

That namespace belongs to Hermes approval handling.

Use a plugin-specific namespace:

```text
hermes_lark_action
```

This helps the generic Feishu card-action fallback route the event without colliding with approval.

---

## 9. Outgoing Clarify Hook

Locate the actual current Hermes clarify delivery path in `gateway/run.py`.

Do not assume line numbers.

Find the semantic area around:

```text
clarify callback
adapter.send_clarify
register
wait_for_response
numbered text fallback
```

Add a narrow patch/hook.

Suggested plugin hook:

```python
on_clarify_requested(...)
```

Possible data:

```python
chat_id
reply_to_message_id
session_key
owner_user_id
question
choices
multi_select
clarify_id
```

Behavior:

```text
if not plugin enabled:
    return False

if platform is not Feishu/Lark:
    return False

if clarify_style != card:
    return False

if no choices:
    return False

if multi_select in V1:
    return False

try send card
success:
    register presentation state
    return True
failure:
    return False
```

`False` means:

```text
Hermes continues its original text fallback
```

That fallback guarantee is mandatory.

---

## 10. Generic Feishu Card Action Bridge

Prefer the existing Hermes Feishu generic card-action fallback.

Do not rewrite WebSocket transport unless current upstream code proves it unavoidable.

Unknown card actions already become a generic synthetic card command/event.

Add a narrow intercept before that event becomes normal command processing.

Important:

- inspect `event.raw_message`;
- parse the real action value;
- only intercept values containing the expected plugin action key;
- do not trust arbitrary user-typed `/card ...` strings as equivalent to a real callback.

Suggested hook:

```python
on_feishu_interaction_action(...)
```

Return a consumed boolean.

---

## 11. Single-Select Callback

Input:

```json
{
  "hermes_lark_action": "clarify_select",
  "clarify_id": "...",
  "response": "..."
}
```

Validation:

1. presentation registry entry exists;
2. callback chat matches;
3. callback operator is allowed to answer;
4. action is still pending.

Then:

```python
from tools.clarify_gateway import resolve_gateway_clarify
```

Call:

```python
resolved = resolve_gateway_clarify(
    clarify_id,
    response,
)
```

If `resolved is True`:

- mark UI answered;
- update original card;
- remove buttons.

If `False`:

- do not create a new turn;
- report expired/already handled;
- keep idempotent behavior.

---

## 12. Answered Card State

Example:

```text
┌─────────────────────────────
│ ✅ 已选择              已回答
│
│ 你希望如何管理这个 Fork？
│
│ 选择：
│ 合并定制版本到 main
└─────────────────────────────
```

Remove active buttons.

Store enough state to make repeated callback handling harmless.

---

## 13. Other Button

Input:

```json
{
  "hermes_lark_action": "clarify_other",
  "clarify_id": "..."
}
```

Call:

```python
mark_awaiting_text(clarify_id)
```

On success update card:

```text
✏️ 等待文字回答

请直接回复你的自定义答案。
```

Then stop handling.

The next normal user text must be handled by Hermes' existing clarify text interception.

Do not add a second text-capture implementation.

---

## 14. Identity and Chat Validation

A group member must not be able to answer someone else's pending clarify unless Hermes' existing semantics explicitly permit it.

Store and validate:

```text
chat_id
initiating user identity
session identity when available
```

Prefer Hermes/Feishu authoritative IDs such as open_id/user_id according to the current adapter's established identity model.

Do not weaken existing platform allowlist behavior.

---

## 15. Idempotence

### Same choice double click

```text
A
A
```

Only first click resolves.

### Conflicting clicks

```text
A
then B
```

Second click cannot overwrite the first result.

### Expired click

After timeout:

```text
click
```

must not restart or resolve a dead request.

### Old card

If a stale card exists after session cleanup:

- callback must fail safely;
- no new turn;
- no exception propagated into gateway processing.

---

## 16. V1 Unsupported Cases

### Open-ended clarify

If `choices` is empty:

```text
return False
```

Use Hermes original text flow.

### Multi-select

If:

```text
multi_select=True
```

in V1:

```text
return False
```

Use Hermes original text flow.

This keeps V1 small and low-risk.

---

## 17. V2 — Multi-Select CardKit Form

After V1 is stable, add a separate implementation increment.

Reference OpenClaw AskUserQuestion pattern:

```text
form
  checker / multi_select_static
  submit button
```

Example:

```text
☑ Streaming Card
☐ Tool Panel
☑ Footer

[提交]
```

Submit once.

Return the exact response representation expected by Hermes' existing multi-select parser.

Do not change Hermes parser.

---

## 18. V2 — Open-Ended Input Form

Optional later enhancement:

```text
input
+
submit
```

Example:

```text
❓ 请输入你的回答

[________________]

[提交]
```

This should still resolve via:

```python
resolve_gateway_clarify(...)
```

No synthetic normal turn.

---

## 19. Patcher Markers

Suggested markers:

```text
HERMES_LARK_CLARIFY_SEND_BEGIN
HERMES_LARK_CLARIFY_SEND_END

HERMES_LARK_CLARIFY_ACTION_BEGIN
HERMES_LARK_CLARIFY_ACTION_END
```

Update:

- patch;
- verify;
- unpatch;
- repair;
- compatibility tests.

Use semantic AST/function anchors.

---

## 20. Tests

### Builder tests

Assert:

- blue header;
- pending status;
- correct number of buttons;
- button values contain `hermes_lark_action`;
- canonical response text is preserved;
- Other action uses `clarify_other`.

### Send success

When card send succeeds:

- hook returns `True`;
- Hermes fallback is suppressed;
- registry entry exists.

### Send failure

When card send fails:

- hook returns `False`;
- Hermes fallback path remains available;
- no pending presentation state causes a hang.

### Select callback

Mock:

```python
resolve_gateway_clarify
```

Assert exact `clarify_id` and response.

### Other callback

Mock:

```python
mark_awaiting_text
```

Assert card is changed to awaiting text.

### Wrong user

Assert:

- resolver not called.

### Wrong chat

Assert:

- resolver not called.

### Double click

Assert resolver succeeds only once / second path is treated as stale.

### Expired clarify

Resolver returns false.

Assert:

- no synthetic new agent turn;
- card/action receives safe stale response behavior.

### Non-plugin card action

Must continue Hermes' existing generic card action flow.

### Approval action

Existing:

```text
hermes_action
```

must not be intercepted by clarify bridge.

---

## 21. Manual Feishu Verification

Trigger a real single-select clarify.

Verify:

- no numbered text message appears;
- button card appears;
- button labels are readable;
- clicking an option resumes the blocked Hermes turn;
- no extra `/card` message becomes visible;
- card updates to answered;
- second click does nothing harmful.

Trigger Other:

- click Other;
- card says waiting for text;
- send arbitrary text;
- Hermes resumes correctly.

Verify group chat identity behavior.

Verify thread/reply placement.

---

## 22. Failure Requirements

Clarify card failures must always degrade to Hermes' text fallback.

Forbidden failure:

```text
card fails
+
fallback suppressed
+
agent waits forever
```

The hook must only claim the prompt when successful delivery is known.

---

## 23. Commit Boundary

Recommended:

```text
feat: render Feishu clarify choices as interactive cards
```

Do not implement Approval or Progress in this Goal.

V2 multi-select/form work should be a later commit if it materially expands scope.

---

## 24. Definition of Done

Phase 2 V1 is complete when:

- config defaults to text;
- single-select choice prompt can become a card;
- click resolves the existing Hermes clarify;
- no normal synthetic user turn is created;
- Other enters Hermes text-capture mode;
- wrong user/chat cannot resolve;
- double click is harmless;
- expired callbacks are harmless;
- card failure falls back to numbered text;
- non-plugin card actions are unaffected;
- approval buttons are unaffected;
- automated tests pass;
- real Feishu single-select and Other flows are verified.
