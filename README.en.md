# Hermes Lark Streaming

[![PyPI](https://img.shields.io/badge/python-%E2%89%A53.11-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Real-time streaming card plugin for [Hermes](https://github.com/NousResearch/hermes-agent) Gateway via Feishu/Lark CardKit v2.0.

Inspired by [openclaw-lark](https://github.com/larksuite/openclaw-lark) and [hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card).

[中文文档](README.md)

![](assets/cover.jpg)

---

## Features

- **Streaming output** — AI responses rendered in real-time interactive cards with typewriter effect
- **Streaming cards** — Dynamically renders thinking, tool calls, and answer elements in event arrival order within a single card
- **Reasoning display** — Shows model thinking/reasoning content
- **Tool use tracking** — Live tool call status with standard icons, result/error blocks
- **CardKit v2.0** — Uses Feishu CardKit streaming API; card creation failures yield to the Hermes Gateway default reply
- **Completion card** — Final card with token usage, duration, and context info
- **Card style** — Configurable card header/footer toggle and body/footer text sizes
- **Message guard** — Auto-terminates updates when message is deleted/recalled
- **Image resolution** — Detects markdown image references, downloads and re-uploads as Feishu img_key
- **Abort handling** — Gracefully handles `/stop` command and message interrupts with aborted state card and automatic new session
- **Cron card delivery** — Delivers scheduled job results as Feishu cards, preserving Markdown rendering
- **Background task card delivery** — Delivers `/background` (`/btw`) task results as cards, with topic-aware reply
- **i18n** — Built-in Chinese/English bilingual card text (status, tool panel, thinking labels, etc.) that auto-switches based on Feishu client language

---

## Card Rendering

The plugin dynamically renders thinking, tool call, and answer elements in event arrival order, keeping multi-round content in its actual order.

When long conversations or excessive tool steps cause the card to approach Feishu's 200-element limit, it automatically splits into multiple cards: the old card is sealed with complete data, a new card continues output, and only the last card includes the footer. Oversized tool panels are also split at step boundaries.

![](assets/streaming.jpg)

---

## Requirements

- Hermes `>= 0.14.0` (2026.5.16) with Feishu/Lark platform configured
- `Python >= 3.11`
- `lark-oapi >= 1.4.0` — Feishu/Lark official Python SDK
- `PyYAML >= 6.0` — YAML parser
- Feishu app permissions: CardKit read/write, message send & reply, image upload

---

## Installation

For the full installation procedure see [INSTALL.md](INSTALL.md).

### AI Agent install

Have the AI agent connected to Hermes read the installation guide and execute it:

```
curl https://raw.githubusercontent.com/Cheerwhy/hermes-lark-streaming/main/INSTALL.md
```

---

## Configuration

Add to `~/.hermes/config.yaml`:

```yaml
streaming:
  enabled: true
```

See [CONFIGURATION.md](CONFIGURATION.md) for the complete configuration reference, defaults, profile scope, and the function controlled by each parameter.

### Credentials

Credentials are resolved in the following order:

| Priority | Source | Variables |
|----------|--------|-----------|
| 1 | Environment | `FEISHU_APP_ID` / `FEISHU_APP_SECRET` (or `LARK_APP_ID` / `LARK_APP_SECRET`) |
| 2 | Config file | `feishu` or `lark` section in `~/.hermes/config.yaml` |

```env
FEISHU_APP_ID=cli_xxxxx
FEISHU_APP_SECRET=xxxxx
```

### Card Style

For a quick starting point, use this minimal example:

```yaml
streaming:
  enabled: true
  progress_mode: text
  width_mode: default
  header:
    enabled: false
  body:
    text_size: normal_v2
  footer:
    enabled: true
    text_size: notation
    fields:
      - [status, elapsed, context, model]
    show_label: false
  panel_expanded: false
display:
  platforms:
    feishu:
      show_tool_use: true
```

See [CONFIGURATION.md](CONFIGURATION.md) for defaults, fallback behavior, profile scope, credentials, hot-reload boundaries, and supported footer fields.

---

## CLI Commands

```bash
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
$HERMES_PYTHON -m hermes_lark_streaming verify     # Verify compatibility (no file changes)
$HERMES_PYTHON -m hermes_lark_streaming install    # Inject hooks
$HERMES_PYTHON -m hermes_lark_streaming uninstall  # Remove hooks
$HERMES_PYTHON -m hermes_lark_streaming restore    # Restore original files from backup
$HERMES_PYTHON -m hermes_lark_streaming status     # Show status (incl. Hermes Python/install dir detection)
```

---

## Update

```bash
cd hermes-lark-streaming
git pull
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
$HERMES_PYTHON -m pip install -e .
$HERMES_PYTHON -m hermes_lark_streaming uninstall   # Remove old injection first
$HERMES_PYTHON -m hermes_lark_streaming verify
$HERMES_PYTHON -m hermes_lark_streaming install
hermes gateway restart
```

---

## Uninstall

```bash
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
$HERMES_PYTHON -m hermes_lark_streaming uninstall
$HERMES_PYTHON -m pip uninstall hermes-lark-streaming
```

---

## How It Works

The plugin injects hook calls into `gateway/run.py` and `cron/scheduler.py` via AST patching. All business logic lives in the `hermes_lark_streaming` package.

**Message flow:**

```
User sends message
  → Card session created
  → Streaming updates (tool status, text — throttled)
  → Image URL async resolution
  → Completion card (tokens, duration, context)
```

If a message is deleted/recalled, updates are auto-terminated.

**Interrupt handling:**

- `/stop` abort — User actively stops generation, card shows interrupted state:

![](assets/abort.jpg)

- Message interrupt — User sends a new message while a response is in progress; old card shows interrupted state, and a new streaming card is automatically created for the new message:

![](assets/interrupt.jpg)

---

## Notes

- `install` modifies `~/.hermes/hermes-agent/gateway/run.py` and `cron/scheduler.py`, and creates `.hermes_lark.bak` backups
- Re-run `verify` + `install` after Hermes updates
- The plugin complements the built-in Feishu adapter: plugin handles streaming cards, built-in adapter handles message routing
- Only affects Feishu/Lark platform — other platforms are unaffected

## Contributors

Thanks to our contributors for their issues and pull requests:

<a href="https://github.com/Mxin-9527"><img src="https://avatars.githubusercontent.com/u/178271393?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/gitteeee"><img src="https://avatars.githubusercontent.com/u/128769493?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/Bandersnatch0x"><img src="https://avatars.githubusercontent.com/u/13325067?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/runfali"><img src="https://avatars.githubusercontent.com/u/39327978?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/thunderfight127-svg"><img src="https://avatars.githubusercontent.com/u/275854191?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/willggy"><img src="https://avatars.githubusercontent.com/u/74762604?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/atomperson"><img src="https://avatars.githubusercontent.com/u/14934637?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/linjunxin01"><img src="https://avatars.githubusercontent.com/u/63715504?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/mouxangithub"><img src="https://avatars.githubusercontent.com/u/48978046?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/numuly"><img src="https://avatars.githubusercontent.com/u/137970054?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/wzgrx"><img src="https://avatars.githubusercontent.com/u/39661556?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/zhaomingcheng01"><img src="https://avatars.githubusercontent.com/u/46734892?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>

---

## License

[MIT](LICENSE)
