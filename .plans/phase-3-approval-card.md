# Phase 3 — OpenClaw-Style Hermes Approval Card

## 1. Goal

Keep Hermes' official execution approval lifecycle and security semantics while changing the Feishu card presentation to an OpenClaw-like style.

The backend must remain Hermes.

Target presentation:

```text
┌──────────────────────────────
│ 🔒 需要执行授权         待确认
│
│ Hermes 请求执行：
│ <command / operation preview>
│
│ [允许一次] [本会话]
│ [始终允许] [拒绝]
└──────────────────────────────
```

---

## 2. Non-Negotiable Backend Semantics

Preserve Hermes' original:

```text
approval_id
pending approval state
operator validation
chat validation
session semantics
resume behavior
```

Preserve four permission choices:

```text
Allow Once
Session
Always
Deny
```

Never simplify to:

```text
Confirm
Reject
```

because that would remove scope semantics.

---

## 3. Preferred Architecture

Use presentation transformation rather than a new approval runtime.

Preferred flow:

```text
Hermes FeishuAdapter.send_exec_approval()
        ↓
Hermes creates official card data
        ↓
plugin transforms presentation
        ↓
Hermes sends card
        ↓
Hermes keeps original _approval_state
        ↓
Hermes original card-action callback
        ↓
Hermes resolve
```

Conceptually:

```text
换皮，不换发动机
```

---

## 4. Configuration

Recommended:

```yaml
display:
  platforms:
    feishu:
      confirmation_style: openclaw
```

Default:

```text
hermes
```

Accepted:

```text
hermes
openclaw
```

Invalid values fall back to:

```text
hermes
```

---

## 5. Avoid Rebuilding Button Payloads

The safest transformer must preserve original button `value` payloads.

For each original approval button retained in the transformed card:

```python
transformed_button["value"] == original_button["value"]
```

must hold semantically.

Important original values include:

```text
hermes_action
approval_id
```

If Hermes adds fields later, the transformer should preserve them automatically where possible.

Do not manually recreate only the fields known today unless necessary.

---

## 6. Feishu Adapter Patch Target

This phase may add a patch target:

```text
plugins/platforms/feishu/adapter.py
```

Locate:

```text
FeishuAdapter.send_exec_approval
```

semantically.

Do not patch by line number.

Insert a small presentation hook after card construction and before send.

Suggested hook:

```python
transform_exec_approval_card(...)
```

Behavior:

```text
if plugin disabled:
    original card

if confirmation_style != openclaw:
    original card

try:
    transform presentation
except:
    original card
```

Transformation failure must never suppress the approval request.

---

## 7. Patcher Requirements

Suggested marker:

```text
HERMES_LARK_APPROVAL_UI_BEGIN
HERMES_LARK_APPROVAL_UI_END
```

Add compatibility validation for the adapter target.

If current Hermes upstream changes the method so the expected semantic anchor disappears:

```text
do not patch blindly
```

Report compatibility failure.

---

## 8. OpenClaw-Like Visual Structure

Recommended pending card:

### Header

```text
🔒 需要执行授权
```

Template:

```text
orange
```

Status:

```text
待确认
```

### Body

Include:

- operation/tool type if available;
- description;
- command or preview;
- reason/context if Hermes provides it.

Do not fabricate metadata Hermes does not provide.

### Buttons

Suggested styles:

```text
Allow Once → primary
Session    → default
Always     → default
Deny       → danger
```

Use the original buttons as source of truth.

---

## 9. Do Not Add Missing Permission Options

Hermes may conditionally omit options.

Examples:

```text
allow_session=False
smart_denied=True
```

If the official card does not include:

```text
Session
Always
```

the transformed card must not invent them.

The original action set is authoritative.

---

## 10. Risk Styling

V1:

```text
orange header
```

for all approval prompts.

Do not invent a risk classifier.

A red/danger header may be added later only if Hermes provides a reliable explicit risk signal.

Do not classify commands by naive string matching.

---

## 11. Preview Formatting

Use CardKit-friendly formatting.

For terminal commands:

```text
monospace / fenced markdown
```

for readable previews.

For file/tool operations:

- show concise description;
- show available path/operation details;
- do not expose secrets that Hermes intentionally redacts.

Preserve Hermes' existing redaction behavior.

---

## 12. Resolved Card State

If current Hermes already updates/replaces the approval card after callback, preserve that mechanism.

Optional presentation hook may style resolved state:

### Approved

```text
✅ 已授权
Approved once / session / permanently
```

### Denied

```text
❌ 已拒绝
```

But this is secondary.

If styling the resolved state requires invasive callback-handler patching, V1 may leave the resolved style closer to Hermes default.

Do not rewrite callback lifecycle solely for cosmetic parity.

---

## 13. Callback Must Stay Official Hermes

The plugin should not add a new approval callback resolver.

Existing Hermes card actions such as:

```json
{
  "hermes_action": "approve_once",
  "approval_id": "..."
}
```

must continue to go directly through Hermes' existing handler.

Do not intercept these through the Clarify generic plugin-action bridge.

---

## 14. Failure Behavior

If transform fails:

```text
send original Hermes card
```

If plugin config cannot be read:

```text
send original Hermes card
```

If OpenClaw-style builder emits invalid card structure:

```text
fallback original card
```

Forbidden:

```text
approval backend remains blocked
because presentation transform prevented sending
```

---

## 15. Tests

### Config default

Assert:

```text
confirmation_style == hermes
```

when unset.

### Original card passthrough

With:

```text
confirmation_style=hermes
```

assert original card is unchanged.

### Transform structure

With:

```text
openclaw
```

assert:

- orange header;
- expected title;
- expected pending state;
- button styles;
- preview retained.

### Payload preservation

For each transformed approval button:

```python
value_before == value_after
```

or exact semantic equality.

### Conditional options

Original with only:

```text
Allow Once
Deny
```

must remain exactly those semantic choices.

### Transform exception

Force transformer failure.

Assert original card is returned.

### Approval callback regression

Existing Hermes approval action still resolves through original code path.

### Clarify separation

`hermes_lark_action` and `hermes_action` remain separate.

### Patcher tests

Assert:

- apply;
- verify;
- reapply idempotence;
- unpatch;
- missing anchor safe failure.

---

## 16. Manual Feishu Verification

Trigger an actual exec approval.

Verify all visible choices that Hermes currently provides.

For each available choice:

### Allow Once

Verify:

- current operation resumes;
- scope is one operation only.

### Session

Verify:

- subsequent allowed operations in the same session follow Hermes behavior;
- new session is not silently granted unless Hermes specifies otherwise.

### Always

Verify:

- Hermes' persistent permission semantics remain unchanged.

### Deny

Verify:

- operation is denied;
- waiting thread resumes with denial behavior;
- no accidental execution.

Also verify:

- group chat operator restrictions;
- stale/duplicate click behavior;
- thread placement;
- no secret leakage in previews.

---

## 17. Files Likely Involved

Plugin:

```text
hermes_lark_streaming/config.py
hermes_lark_streaming/cardkit/interaction_builder.py
hermes_lark_streaming/patch.py
hermes_lark_streaming/patcher.py
tests/*
```

Hermes patch target:

```text
plugins/platforms/feishu/adapter.py
```

Do not modify Hermes backend source outside the narrowly injected presentation hook.

---

## 18. Upstream Compatibility

Extend compatibility checks to verify the semantic approval patch anchor.

If Hermes changes:

```text
send_exec_approval
```

CI should clearly report:

```text
Feishu approval presentation compatibility broken
```

rather than silently producing a partial patch.

---

## 19. Commit Boundary

Recommended:

```text
feat: add OpenClaw-style Hermes approval presentation
```

Do not include Progress in this Goal.

Do not refactor Clarify unless required for a shared CardKit builder utility and the change is demonstrably behavior-neutral.

---

## 20. Definition of Done

Phase 3 is complete when:

- default approval presentation remains Hermes;
- OpenClaw style can be enabled explicitly;
- original `approval_id` and action values are preserved;
- Allow Once / Session / Always / Deny semantics remain intact;
- omitted Hermes options are not invented;
- original Hermes callback handler remains responsible for resolution;
- transform failure returns original card;
- wrong operator/chat protections remain intact;
- automated tests pass;
- real Feishu approval choices are manually verified.
