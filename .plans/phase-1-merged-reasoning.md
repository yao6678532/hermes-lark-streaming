# Phase 1 — Single Merged Reasoning Lane

## 1. Goal

Implement a backward-compatible merged reasoning presentation mode on `yao-custom`.

New configuration:

```yaml
streaming:
  reasoning_mode: merged
```

Default:

```yaml
streaming:
  reasoning_mode: segmented
```

Given the event sequence:

```text
Reasoning A
Tool 1
Reasoning B
Tool 2
Reasoning C
Answer
```

the internal event chronology must remain:

```text
R1 → T1 → R2 → T2 → R3 → A
```

but Feishu must display exactly one reasoning region containing:

```text
A + B + C
```

The final card must contain exactly one collapsible reasoning panel.

---

## 2. Scope

This phase includes:

- config support for `reasoning_mode`;
- protected footer regression tests;
- session-scoped merged reasoning presentation state;
- merged streaming renderer path;
- merged final renderer path;
- timing semantics;
- relevant tests and compatibility checks.

This phase does **not** include:

- Clarify cards;
- Approval redesign;
- Working progress integration;
- removal of `SegmentState`;
- full OpenClaw `PresentationState`;
- Hermes backend changes.

---

## 3. Protected Baseline

Before implementation, confirm the existing `yao-custom` footer behavior and add tests if necessary.

Protect:

- GPT/Codex quota footer;
- quota-present hides context;
- compact completed footer;
- fail-open quota lookup;
- non-GPT footer behavior.

Recommended first commit if needed:

```text
test: protect yao-custom footer behavior
```

---

## 4. Existing State Model

Current `SegmentState` records flat chronological segments.

Example:

```text
R1
T1
R2
T2
R3
A
```

A new reasoning segment is currently created whenever the previous segment is not reasoning.

This is why reasoning after a tool call currently creates another Feishu panel.

Do not remove this behavior from the chronology layer.

---

## 5. Add MergedReasoningState

Recommended file:

```text
hermes_lark_streaming/streaming/reasoning.py
```

Suggested state:

```python
class MergedReasoningState:
    text: str
    active_since: float | None
    elapsed_ms: float
    created: bool
    dirty: bool
```

Suggested behavior:

```text
append(text)
pause()
finalize()
reset_render_state()
```

### append(text)

- ignore empty input;
- if `active_since is None`, start a new active reasoning interval;
- append text;
- mark dirty.

### pause()

- if an active interval exists, add elapsed time;
- clear `active_since`;
- preserve all text.

### finalize()

- call pause;
- preserve final content.

### reset_render_state()

Used only if CardKit state must be rebuilt after a card split/recreate:

- `created = False`;
- `dirty = True`;
- do not clear text or timing.

---

## 6. CardSession Integration

Modify:

```text
hermes_lark_streaming/streaming/session.py
```

Add:

```python
self.merged_reasoning = MergedReasoningState()
```

Keep:

```python
self.segment_state = SegmentState()
```

Target conceptual structure:

```text
CardSession
├── segment_state
├── merged_reasoning
├── tool_use
├── footer
└── CardKit runtime state
```

---

## 7. Configuration

Modify:

```text
hermes_lark_streaming/config.py
```

Add:

```python
@property
def reasoning_mode(self) -> str:
    ...
```

Accepted:

```text
segmented
merged
```

Fallback:

```text
segmented
```

Invalid config must never crash the plugin.

---

## 8. Unified Reasoning Ingestion

Current reasoning may arrive through more than one path, including:

```text
on_reasoning()
_on_thinking_segment()
```

Create a shared internal helper when useful:

```python
_append_reasoning(session, text)
```

Its job:

```text
always:
    SegmentState.on_reasoning_delta(text)

if reasoning_mode == merged:
    MergedReasoningState.append(text)
```

Avoid double append.

Do not assume native reasoning and think-tag reasoning are semantically identical without checking the existing call chain.

---

## 9. Tool Semantics

When a tool actually starts:

```text
merged_reasoning.pause()
```

Then continue existing tool tracking:

```text
tool_use.record_start(...)
segment_state.on_tool_event(...)
```

Tool completion:

- update tool state as today;
- do not clear merged reasoning;
- do not create another merged reasoning lane.

If later reasoning arrives:

```text
append()
```

must continue using the same accumulated text.

---

## 10. Answer Semantics

Before processing the first valid answer delta:

```text
merged_reasoning.pause()
```

Then continue current answer handling.

Do not clear merged reasoning.

Completion:

```text
merged_reasoning.finalize()
```

The final reasoning panel uses accumulated text and accumulated reasoning-active duration.

---

## 11. Timing Semantics

Preferred behavior:

```text
reasoning active time
=
sum of reasoning intervals
```

Tool execution duration should not inflate the Thought duration.

Example:

```text
R active 3s
Tool 20s
R active 4s
```

Expected:

```text
Thought for ~7s
```

not:

```text
Thought for ~27s
```

If exact active-time accumulation causes disproportionate complexity, document the tradeoff before simplifying.

---

## 12. Fixed CardKit Element IDs

Reuse:

```python
REASONING_ELEMENT_ID = "reasoning_content"
REASONING_TEXT_ELEMENT_ID = "reasoning_text"
```

Merged mode must not create:

```text
reasoning_0_panel
reasoning_2_panel
reasoning_4_panel
```

for the live UI.

---

## 13. First Merged Reasoning Render

On the first reasoning content in merged mode:

```text
CardKit batch update
↓
add one collapsible reasoning panel
```

Use:

```text
element_id = reasoning_content
text element_id = reasoning_text
```

Then:

```python
session.merged_reasoning.created = True
```

Only mark created after successful CardKit creation/update.

---

## 14. Subsequent Merged Reasoning Updates

All later reasoning updates must target:

```text
reasoning_text
```

Example:

```python
cardkit_stream_element(
    card_id,
    REASONING_TEXT_ELEMENT_ID,
    merged_text,
    sequence=...
)
```

On success:

```python
dirty = False
```

If another reasoning delta arrives during the request, preserve dirty state correctly.

Follow the existing controller's race-safety patterns.

---

## 15. Segment Dirty / Created Handling

The chronology still creates individual reasoning segments.

In merged mode those reasoning segments are not individual UI panels.

Prevent these chronology segments from causing:

- repeated `add_elements`;
- endless flush loops;
- permanently dirty state;
- incorrect CardKit element counts.

The merged UI's creation truth should be:

```text
session.merged_reasoning.created
```

not:

```text
individual reasoning segment.created
```

It is acceptable to mark reasoning chronology segments as UI-consumed after successful merged rendering, while retaining their text and chronological identity.

Document the exact choice in code comments/tests.

---

## 16. Streaming Controller Strategy

Modify:

```text
hermes_lark_streaming/streaming/controller.py
```

Do not rewrite the entire `_do_flush()`.

Prefer a narrow split:

```python
if self._cfg.reasoning_mode == "merged":
    # merged reasoning-specific handling
else:
    # existing segmented reasoning behavior
```

Tool and answer paths should remain shared where practical.

---

## 17. Complete Card Builder

Modify:

```text
hermes_lark_streaming/cardkit/builder.py
```

Current segmented behavior remains unchanged when:

```text
reasoning_mode == segmented
```

Merged mode should accept or derive:

```text
merged_reasoning_text
merged_reasoning_elapsed_ms
```

Then append exactly one reasoning panel.

Do not render one panel per historical reasoning segment in merged mode.

`panel_expanded` must continue to control the completed-panel initial state.

---

## 18. Split Card Semantics

Explicitly test and define behavior when the card splits near element limits.

Merged reasoning is presentation state for the active card.

When a split/recreate requires rebuilding UI:

```text
reset_render_state()
```

may mark the fixed reasoning element as needing recreation.

Do not lose accumulated reasoning text.

Do not duplicate reasoning across multiple active elements in the same resulting card.

If the existing split model intentionally preserves previous completed content in an older card, keep that behavior and document how merged reasoning is represented across the split boundary.

Prefer the smallest behavior-preserving implementation.

---

## 19. Failure Semantics

Preserve existing fail-open behavior for:

- card create failure;
- batch update failure;
- stream element failure;
- missing element ID;
- rate limiting;
- streaming closed state.

Merged rendering must not turn a UI failure into a failed Hermes agent run.

Do not solve failures by swallowing every exception or disabling tests.

---

## 20. Tests

Recommended files:

```text
tests/test_segments.py
tests/test_controller.py
tests/test_cardkit_builder.py
tests/test_merged_reasoning.py
```

Use the existing project structure rather than forcing new files if equivalent test modules already exist.

### Case A — R → A

Input:

```text
R1
A
```

Assertions:

- one live merged reasoning panel;
- one fixed reasoning text element;
- final card has one reasoning panel.

### Case B — R → T → R → A

Internal:

```text
R T R A
```

Assertions:

- chronology contains two reasoning segments;
- merged UI contains one reasoning panel;
- merged text contains both reasoning fragments in order.

### Case C — R → T → R → T → R → A

Assertions:

- no second merged reasoning panel is created;
- only fixed reasoning IDs are used by the merged UI;
- final card contains one panel.

### Case D — segmented mode

```yaml
reasoning_mode: segmented
```

Assertions:

- current v0.12.0 behavior remains unchanged;
- existing tests remain valid.

### Case E — reasoning hidden

```yaml
show_reasoning: false
```

Assertions:

- no reasoning UI;
- no crash;
- answer continues.

### Case F — tools hidden

```yaml
show_tool_use: false
```

Assertions:

- actual tool state still changes;
- merged reasoning continues across tool events;
- no tool panel shown.

### Case G — timing

Use controllable time / monkeypatch.

Assertions:

```text
reasoning active intervals are accumulated
tool wait is excluded
```

### Case H — CardKit failure

Simulate failed add/stream.

Assertions:

- state remains recoverable/fail-open;
- no infinite flush loop;
- agent path is not broken.

### Case I — footer regression

Assertions:

- existing `yao-custom` completed footer remains identical in semantics.

---

## 21. Files to Inspect

Likely files:

```text
hermes_lark_streaming/config.py
hermes_lark_streaming/controller.py
hermes_lark_streaming/streaming/session.py
hermes_lark_streaming/streaming/segments.py
hermes_lark_streaming/streaming/controller.py
hermes_lark_streaming/streaming/segment_helper.py
hermes_lark_streaming/cardkit/builder.py
tests/*
```

Do not modify a file solely because it appears on this list.

---

## 22. Required Verification

Before implementation:

```text
verify branch = yao-custom
verify working tree
read AGENTS.md
run baseline tests
```

After implementation:

Run repository-configured:

```text
pytest
ruff
mypy
compatibility checks
```

If a tool is not configured, state that rather than inventing a result.

---

## 23. Manual Feishu Verification

Use a task that forces:

```text
reasoning
tool
reasoning
tool
reasoning
answer
```

Configuration:

```yaml
streaming:
  reasoning_mode: merged

display:
  platforms:
    feishu:
      show_reasoning: true
      show_tool_use: false
```

Verify:

- one live reasoning region throughout;
- reasoning updates continue after tool calls;
- tools still execute;
- no visible tool panels;
- final card has one collapsed reasoning panel;
- final answer is complete;
- custom footer remains correct;
- `/stop` and interruption still behave normally.

---

## 24. Commit Boundary

Recommended commit:

```text
feat: add merged reasoning presentation mode
```

If footer tests are added separately:

```text
test: protect yao-custom footer behavior
feat: add merged reasoning presentation mode
```

Do not start Phase 2 in the same Goal.

---

## 25. Definition of Done

Phase 1 is complete only when:

- `reasoning_mode` exists and defaults to `segmented`;
- merged mode preserves internal chronology;
- one live reasoning UI region exists per active card;
- tool interruption does not create another merged reasoning panel;
- merged reasoning text is complete and correctly ordered;
- Thought duration semantics are tested;
- final card contains one reasoning panel;
- segmented mode has regression coverage;
- `show_reasoning=false` has regression coverage;
- `show_tool_use=false` has regression coverage;
- answer/image/split/stop/interruption/follow-up behavior does not regress;
- custom `yao-custom` footer does not regress;
- automated tests pass;
- manual Feishu verification items are reported.
