# Tool Panel and Run Details Maintenance

This branch-owned document is the maintenance map for the UI work added on
`feat/run-details`.  It complements, but does not replace,
[`CONFIGURATION.md`](../CONFIGURATION.md): use that file for supported values and
this file to locate the code and tests behind a presentation change.

## Configuration ownership

| Feature | User configuration | Configuration reader | Runtime effect |
|---|---|---|---|
| Tool Panel visibility | `display.platforms.feishu.show_tool_use` (fallback: `display.show_tool_use`) | `Config.show_tool_use` in `hermes_lark_streaming/config.py` | Hides or shows the unified Tool Panel without changing Hermes tool execution. Reread at runtime. |
| Tool step detail | `display.platforms.feishu.show_tool_detail` (fallback: `display.show_tool_detail`) | `Config.show_tool_detail` | Hides or shows each step's secondary detail, while retaining title, status, elapsed time, and result/error blocks. Reread at runtime. |
| Tool detail compacting | `display.platforms.feishu.tool_detail_mode`: `full` or `compact` (fallback: `display.tool_detail_mode`) | `Config.tool_detail_mode` | Chooses sanitized full detail or compact command-oriented detail. Reread at runtime. |
| Run Details on completion | `streaming.footer.enabled` | `Config.footer_enabled` | Enables the terminal, borderless, default-collapsed Run Details panel. Requires a gateway restart after a configuration edit. |
| Run Details typography | `streaming.footer.text_size` | `Config.footer_text_size` | Applies to summary and all expanded detail text. Requires a gateway restart after a configuration edit. |
| Run Details candidate fields | `streaming.footer.fields` | `Config.footer_fields` | Selects candidates for metrics and ordinary details. A missing/empty/default historical policy allows GPT quota promotion; an explicit subset such as `[[context]]` does not. Requires a gateway restart after a configuration edit. |
| Legacy Run Details label flag | `streaming.footer.show_label` | `Config.footer_show_label` | Read for compatibility; current Run Details always render explicit labels. Requires a gateway restart after a configuration edit. |

The complete field vocabulary (`tokens`, `context`, `gpt_quota`, `quota_reset`,
`cache`, `reasoning`, and so on), defaults, profile paths, and YAML examples are
defined in `CONFIGURATION.md`.

## Tool Panel implementation map

| Change needed | Primary location | Supporting location | Regression tests |
|---|---|---|---|
| Hermes tool name, icon, title, sanitizer, result behavior | `hermes_lark_streaming/streaming/tooluse.py`: `_TOOL_DESCRIPTORS`, `_resolve_tool_descriptor()` | — | `tests/test_tooluse.py` |
| Status colors, title/detail/result CardKit rendering | `hermes_lark_streaming/cardkit/builder.py`: `_build_tool_panel()`, `_build_tool_step_*()` | `hermes_lark_streaming/streaming/segment_helper.py` | `tests/test_cardkit.py`, `tests/test_controller.py`, `tests/test_segment_helper.py` |
| Visibility/detail-mode configuration behavior | `hermes_lark_streaming/config.py` | `hermes_lark_streaming/streaming/controller.py` | `tests/test_config.py`, `tests/test_controller.py` |

Descriptor resolution is intentionally exact-match first, followed by prefix
matching.  Keep the neutral unknown-tool fallback distinct from the Skill-family
icon.  Do not change command/path/URL/search sanitization or Tool Panel status
colors while making an icon-only adjustment.

## Run Details implementation map

| Change needed | Primary location | Supporting location | Regression tests |
|---|---|---|---|
| Summary, adaptive metrics, circular chart spec, two-column detail layout, panel presentation | `hermes_lark_streaming/cardkit/builder.py`: `_build_run_details_elements()` and `_build_run_details_*()` helpers | `hermes_lark_streaming/cardkit/i18n.py` for localized text | `tests/test_cardkit.py` |
| GPT weekly quota fetch and reset data | `hermes_lark_streaming/streaming/controller.py` | `hermes_lark_streaming/quota.py` | `tests/test_controller.py` |
| GPT summary threshold source of truth | `hermes_lark_streaming/quota.py`: `_quota_color()` | `cardkit/builder.py`: `_RUN_DETAILS_METRIC_COLOR_POLICIES` | `tests/test_controller.py`, `tests/test_cardkit.py` |
| Footer fields/default-policy behavior | `hermes_lark_streaming/config.py`: `Config.footer_fields` | `cardkit/builder.py`: `_run_details_fields_are_default()` | `tests/test_config.py`, `tests/test_cardkit.py`, `tests/test_controller.py` |

### Current metric policy

- At most two percentage metrics are rendered: GPT weekly quota, then Context,
  then Cache.
- With valid GPT quota and Context, show GPT + Context.  Without GPT, show
  Context + Cache when both are available.  Never create empty metric columns.
- GPT quota uses the weekly window with `limit_window_seconds == 604800`; the
  remaining percentage and `reset_at` must come from that same window.
- GPT quota colors are remaining-based: `>= 50%` green, `>= 20%` orange, and
  `< 20%` red.  Summary text and its circular progress chart share this policy.
- Context colors are usage-risk-based: `< 50%` green, `50–<80%` orange, and
  `>=80%` red.  Cache hit is neutral blue below 80%, green at or above 80%.
- The chart is a 28px CardKit chart with top-level `preview: false`; its VChart
  `circularProgress` spec uses one metric value, `categoryField`, and
  `valueField`. Its desktop hover tooltip uses a metric-specific label and a
  rounded integer percentage, while the chart `value` remains the 0–1 fraction
  required for drawing and threshold mapping. Preserve this shape when changing
  colors or data.

## Deployment decision

| Changed scope | Required action |
|---|---|
| Only `display.*` presentation configuration | The display settings are reread at runtime; no hook reinstall. |
| `streaming.*` configuration | Restart the Hermes gateway so its cached `Config` instance reloads. |
| Tool Panel / Run Details / CardKit / descriptor source code, without `patcher.py` | Deploy the updated plugin and restart the Hermes gateway as appropriate; do **not** run hook uninstall/install solely for these changes. |
| `hermes_lark_streaming/patcher.py` generated-hook changes | Run `uninstall`, then `install`, then restart the Hermes gateway. |

Do not use OpenClaw gateway commands for this Hermes plugin.  `patcher.py` is the
only source that changes the injected Hermes gateway hook code.
