# yao-custom Feishu UX Roadmap

## 1. Project Scope

Repository:

```text
yao6678532/hermes-lark-streaming
```

Target branch:

```text
yao-custom
```

Upstream project:

```text
Cheerwhy/hermes-lark-streaming
```

Primary UX reference:

```text
larksuite/openclaw-lark
```

Hermes runtime/backend source of truth:

```text
NousResearch/hermes-agent
```

This roadmap defines the staged implementation plan for improving the Feishu/Lark experience on `yao-custom`.

The goal is **not** to rewrite Hermes or replace Cheerwhy's streaming architecture wholesale. The goal is to keep Hermes' runtime semantics and security model intact while improving Feishu presentation and interaction.

---

## 2. Current Branch Baseline

`yao-custom` is based on the upstream `hermes-lark-streaming` v0.12.0 line and currently differs primarily by a custom footer implementation.

Existing `yao-custom` behavior is considered a protected baseline.

### Existing custom footer behavior that must not regress

- GPT/Codex quota shown in the completed footer.
- When GPT quota is present, context usage is hidden to keep the footer compact.
- Completed footer uses a compact form such as:

```text
✅ 26.5s · ...
```

instead of:

```text
✅ · 26.5s · ...
```

- Non-GPT models retain the existing footer behavior.
- Footer failure paths must remain fail-open.
- Existing footer field configuration and text sizing must remain supported.

Before implementing new UX work, add regression tests for these customizations if equivalent coverage does not already exist.

---

## 3. Guiding Architecture

The current plugin is segment-centric:

```text
Hermes events
    ↓
SegmentState
    ↓
CardKit elements
```

This roadmap uses a conservative transition toward a more OpenClaw-like presentation model without removing the existing chronological segment history.

Target direction:

```text
Hermes events
    │
    ├── Event chronology
    │      SegmentState
    │      R → T → R → T → R → A
    │
    └── Presentation state
           ├── MergedReasoningState
           ├── ProgressState
           └── Interaction state
                    ↓
                CardKit UI
```

The important separation is:

```text
what happened
≠
how it is presented
```

`SegmentState` remains useful for chronology, diagnostics, tool ordering, split-card handling, and compatibility.

Presentation state is added only where needed.

---

## 4. Core Design Rules

### 4.1 Preserve Hermes backend semantics

Do not reimplement Hermes backend workflows when Hermes already provides them.

Reuse Hermes for:

- clarify pending registry;
- `clarify_id`;
- waiting and timeout behavior;
- text fallback;
- multi-select parsing;
- approval state;
- `approval_id`;
- Allow Once / Session / Always / Deny semantics;
- operator and chat validation;
- resuming blocked agent execution.

### 4.2 OpenClaw is a presentation reference

Borrow from `larksuite/openclaw-lark`:

- one reasoning presentation lane;
- one final collapsible reasoning panel;
- confirmation card layout;
- AskUserQuestion-style input cards;
- pending / answered visual states;
- CardKit form patterns;
- fixed-element updates where appropriate.

Do **not** copy OpenClaw backend semantics when they conflict with Hermes.

### 4.3 Fail open

A presentation-layer failure must not break the Hermes turn.

Examples:

```text
merged reasoning render fails
→ preserve existing text/card fallback

clarify card fails
→ Hermes numbered-text clarify fallback

approval transform fails
→ original Hermes approval card

progress card update fails
→ Hermes original text heartbeat can continue
```

### 4.4 Keep patches narrow

The plugin patches Hermes upstream code. Every new patch point must be:

- semantic, not line-number based;
- idempotent;
- verifiable;
- reversible;
- fail-closed when the expected anchor disappears.

Do not silently patch an unknown upstream layout.

---

## 5. Configuration Strategy

All new behavior must be backward compatible.

Recommended configuration:

```yaml
streaming:
  reasoning_mode: segmented
  progress_mode: text

display:
  platforms:
    feishu:
      clarify_style: text
      confirmation_style: hermes
```

Optional enhanced configuration:

```yaml
streaming:
  reasoning_mode: merged
  progress_mode: card

display:
  platforms:
    feishu:
      show_tool_use: false
      clarify_style: card
      confirmation_style: openclaw
```

Allowed values:

```text
reasoning_mode:
  segmented
  merged

progress_mode:
  text
  card

clarify_style:
  text
  card

confirmation_style:
  hermes
  openclaw
```

Invalid values must fall back to the legacy behavior.

---

# 6. Phase Overview

## Phase 1 — Single Merged Reasoning Lane

Goal:

```text
thinking
→ tool
→ thinking
→ tool
→ thinking
→ answer
```

continues to be recorded internally as:

```text
R1 → T1 → R2 → T2 → R3 → A
```

but Feishu displays one reasoning region:

```text
💭 reasoning
R1 + R2 + R3
```

Key principles:

- preserve `SegmentState`;
- add session-scoped `MergedReasoningState`;
- reuse fixed reasoning CardKit element IDs;
- one live reasoning region;
- one final collapsible reasoning panel;
- keep `segmented` as the default.

Detailed plan:

```text
.plans/phase-1-merged-reasoning.md
```

Recommended commit:

```text
feat: add merged reasoning presentation mode
```

---

## Phase 2 — Feishu Clarify Card

Goal:

Turn Hermes clarify prompts such as:

```text
❓ Which option do you want?

1. A
2. B
3. C

Reply with the number...
```

into an interactive Feishu card:

```text
❓ 需要你的选择

[A]
[B]
[C]
[其他回答…]
```

Clicking a choice must resolve the **existing Hermes clarify request**, not create a synthetic normal user turn.

Reuse:

```text
tools.clarify_gateway.resolve_gateway_clarify()
tools.clarify_gateway.mark_awaiting_text()
```

V1:

- single select;
- Other → text fallback.

V2:

- multi-select form;
- open-ended form input.

Detailed plan:

```text
.plans/phase-2-clarify-card.md
```

Recommended commit:

```text
feat: render Feishu clarify choices as interactive cards
```

---

## Phase 3 — OpenClaw-Style Approval Presentation

Goal:

Keep Hermes' existing approval lifecycle but replace only the Feishu presentation.

Preserve:

```text
Allow Once
Session
Always
Deny
```

Do not reduce these to:

```text
Confirm
Reject
```

The safest approach is:

```text
Hermes builds official approval card
        ↓
plugin presentation transformer
        ↓
same original button values
        ↓
Hermes original callback handler
        ↓
Hermes resumes
```

Detailed plan:

```text
.plans/phase-3-approval-card.md
```

Recommended commit:

```text
feat: add OpenClaw-style Hermes approval presentation
```

---

## Phase 4 — Progress Inside the Streaming Card

Goal:

Stop emitting separate messages such as:

```text
⏳ Working — 3 min — iteration 3/60
```

when an active Feishu streaming card exists.

Instead show the status in a fixed card region:

```text
⏳ 运行 3 min · iteration 3/60
```

The progress region disappears when the final card is produced.

Do not parse ordinary message strings to detect Working notices. Hook the real Hermes long-running progress generation path.

Detailed plan:

```text
.plans/phase-4-progress-card.md
```

Recommended commit:

```text
feat: fold long-running progress into streaming cards
```

---

# 7. Recommended Target Layout

Do not migrate everything at once. Add new modules only as they become necessary.

Target shape:

```text
hermes_lark_streaming/
├── cardkit/
│   ├── builder.py
│   ├── interaction_builder.py
│   └── ...
│
├── interactions/
│   ├── __init__.py
│   ├── clarify.py
│   └── registry.py
│
├── streaming/
│   ├── controller.py
│   ├── session.py
│   ├── segments.py
│   ├── reasoning.py
│   ├── progress.py
│   └── ...
│
├── controller.py
├── config.py
├── patch.py
└── patcher.py
```

---

# 8. Patcher Evolution

Current patch targets already include Hermes gateway / scheduler integration.

Later phases may require new semantic hooks.

Recommended markers:

```text
HERMES_LARK_CLARIFY_SEND_BEGIN
HERMES_LARK_CLARIFY_ACTION_BEGIN
HERMES_LARK_PROGRESS_BEGIN
HERMES_LARK_APPROVAL_UI_BEGIN
```

Whenever a new marker is introduced, update:

- patch application;
- verification;
- unpatch;
- repair;
- tests;
- upstream compatibility checks.

Potential additional Hermes patch target:

```text
plugins/platforms/feishu/adapter.py
```

This should only be added when necessary, especially for approval presentation transformation.

Clarify should preferentially use the existing generic card-action routing and a narrow gateway intercept rather than rewriting the Feishu WebSocket handler.

---

# 9. Required Testing Layers

Every phase requires three layers of testing.

## 9.1 Unit tests

Test pure state and builders.

Examples:

- merged reasoning append/pause/finalize;
- interaction card structures;
- preserved approval button values;
- progress state updates.

## 9.2 Integration-style plugin tests

Test controller and patch flow.

Examples:

- event sequence → CardKit operations;
- clarify button → Hermes clarify resolver;
- failure → legacy fallback;
- patcher anchors and idempotence.

## 9.3 Real Feishu verification

Some behavior cannot be fully proven by unit tests:

- CardKit element update sequencing;
- actual card action callbacks;
- collapsed panel appearance;
- form submit behavior;
- thread/topic routing;
- callback user identity;
- mobile Feishu rendering.

Each phase's file contains its manual verification checklist.

---

# 10. Global Regression Requirements

The following existing behaviors must continue to work throughout all phases:

- streaming answers;
- native reasoning;
- thinking-tag reasoning;
- tool execution;
- `show_tool_use`;
- `show_reasoning`;
- markdown image upload/resolve;
- card splitting;
- CardKit rate-limit handling;
- CardKit missing-element recovery;
- final card fallback;
- `/stop`;
- interrupted runs;
- queued follow-up turns;
- background review;
- cron delivery;
- background delivery;
- multi-profile behavior;
- thread/reply routing;
- current `yao-custom` footer.

---

# 11. Commit Strategy

Do not implement all four phases in one long Codex Goal.

Recommended sequence:

```text
test: protect yao-custom footer behavior

feat: add merged reasoning presentation mode

feat: render Feishu clarify choices as interactive cards

feat: add OpenClaw-style Hermes approval presentation

feat: fold long-running progress into streaming cards

docs: document yao-custom Feishu UX options
```

Review and validate each phase before starting the next.

---

# 12. Codex Execution Strategy

Recommended:

```text
Model: Sol
Mode: Goal
```

Run one Goal per phase.

The startup prompt should tell Codex to:

1. verify `yao-custom`;
2. read `AGENTS.md`;
3. read this roadmap;
4. read only the current phase plan in detail;
5. run baseline tests;
6. sanity-check plan assumptions against current code;
7. use current code as truth when details drift;
8. preserve scope;
9. implement, test, and stop at the phase boundary.

Do not ask Codex to redesign the whole architecture before every phase.

---

# 13. Explicit Non-Goals

This roadmap does not require:

- removing `SegmentState`;
- implementing a full OpenClaw `PresentationState`;
- replacing Hermes approval backend;
- replacing Hermes clarify backend;
- creating a second agent execution engine;
- converting approval into synthetic user messages;
- converting clarify selection into a normal user turn;
- redesigning the custom footer;
- changing Hermes tool execution semantics;
- changing model reasoning behavior.

---

# 14. Final Desired User Experience

## Agent Card

During execution:

```text
🤖 Hermes

💭 思考中…
single live reasoning region

answer streaming

⏳ 3 min · iteration 3/60
```

Completed:

```text
💭 思考了 18.2s
[collapsed]

Final answer

────────────────
✅ 26.5s · GPT quota · model
```

The final footer remains the current `yao-custom` footer.

---

## User Input Card

```text
❓ 需要你的选择        待回答

你希望如何管理这个 Fork？

[保留 main 跟踪官方]
[定制版本合并到 main]
[设定制分支为默认分支]

[其他回答…]
```

Clicking resumes the original Hermes clarify wait.

---

## Approval Card

```text
🔒 需要执行授权        待确认

Hermes 请求执行：
<command>

[允许一次] [本会话]
[始终允许] [拒绝]
```

The backend remains Hermes official approval handling.

---

# 15. Final Definition of Done

The roadmap is complete only when:

- merged reasoning is stable;
- Clarify single-select card works reliably;
- approval presentation is OpenClaw-like without changing permission semantics;
- Working progress no longer pollutes chat when a streaming card is active;
- all legacy fallback paths remain functional;
- `yao-custom` footer has no regression;
- automated test suites pass;
- upstream patch compatibility checks pass;
- real Feishu verification is completed for all four phases;
- documentation describes all new configuration options.
