"""cardkit 包测试 — markdown 优化、表格处理、卡片构建."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from hermes_lark_streaming.cardkit.builder import (
    _LOADING_ELEMENT_ID,
    REASONING_ELEMENT_ID,
    REASONING_TEXT_ELEMENT_ID,
    TOOL_PANEL_ELEMENT_ID,
    _build_footer_elements,
    _build_header,
    _build_reasoning_panel,
    _build_tool_panel,
    _build_tool_step_title,
    _compact,
    _escape_md,
    _format_elapsed,
    _format_quota_reset_at,
    _format_tool_elapsed,
    _longest_backtick_run,
    _run_details_metric_color,
    build_complete_card,
    build_streaming_card_v2,
)
from hermes_lark_streaming.cardkit.interaction_builder import build_approval_card, build_clarify_card
from hermes_lark_streaming.cardkit.markdown import (
    _downgrade_tables,
    _find_tables_outside_code_blocks,
    _split_long_text,
    _strip_invalid_image_keys,
    optimize_markdown_style,
)
from hermes_lark_streaming.interactions.approval import _official_buttons
from hermes_lark_streaming.streaming.progress import ProgressState
from hermes_lark_streaming.streaming.segments import Segment

# --- Markdown 优化 ---


class TestOptimizeMarkdownStyle:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("# Title", "#### Title"),
            ("## Sub", "##### Sub"),
            ("### Deep", "##### Deep"),
        ],
    )
    def test_headings_are_downgraded(self, source: str, expected: str) -> None:
        assert expected in optimize_markdown_style(source)

    def test_h4_h5_h6_unchanged(self) -> None:
        text = "#### H4\n##### H5\n###### H6"
        result = optimize_markdown_style(text)
        assert "#### H4" in result
        assert "##### H5" in result

    def test_heading_in_code_block_preserved(self) -> None:
        text = "```\n# Should not change\n```"
        assert "# Should not change" in optimize_markdown_style(text)

    def test_blank_line_compression(self) -> None:
        result = optimize_markdown_style("a\n\n\n\n\nb")
        assert "\n\n\n" not in result

    def test_invalid_image_key_removed(self) -> None:
        text = "![alt](not_img_key)"
        assert "not_img_key" not in optimize_markdown_style(text)

    def test_valid_img_key_preserved(self) -> None:
        text = "![alt](img_v3_abc123)"
        assert "img_v3_abc123" in optimize_markdown_style(text)

    def test_no_headings_unchanged(self) -> None:
        text = "plain text\nanother line"
        assert optimize_markdown_style(text) == text

    def test_mixed_headings_and_code(self) -> None:
        text = "# Title\n```\n# Code heading\n```\n## Sub"
        result = optimize_markdown_style(text)
        assert "#### Title" in result
        assert "# Code heading" in result


class TestBuildClarifyCard:
    def test_pending_single_select_buttons_use_plugin_namespace(self) -> None:
        card = build_clarify_card(
            clarify_id="clarify-1",
            question="Which path?",
            choices=["A", "B", "C"],
        )

        assert card["schema"] == "2.0"
        assert card["header"]["template"] == "blue"
        assert card["header"]["text_tag_list"][0]["color"] == "blue"
        groups = [item for item in card["body"]["elements"] if item["tag"] == "column_set"]
        assert len(groups) == 1
        group = groups[0]
        assert group["flex_mode"] == "stretch"
        assert len(group["columns"]) == 4
        actions = [column["elements"][0] for column in group["columns"]]
        assert all(action["tag"] == "button" and action["width"] == "fill" for action in actions)
        assert actions[0]["value"] == {
            "hermes_lark_action": "clarify_select",
            "clarify_id": "clarify-1",
            "response": "A",
        }
        assert "hermes_action" not in actions[0]["value"]
        assert actions[-1]["value"] == {
            "hermes_lark_action": "clarify_other",
            "clarify_id": "clarify-1",
        }
        assert all(action.get("type") != "primary" for action in actions)

    @pytest.mark.parametrize("choice_count", [1, 2, 3, 4])
    def test_pending_dynamic_choice_count_keeps_other_last(self, choice_count: int) -> None:
        choices = [f"choice-{index}" for index in range(choice_count)]
        card = build_clarify_card(clarify_id="clarify-1", question="Which path?", choices=choices)
        group = next(item for item in card["body"]["elements"] if item["tag"] == "column_set")
        actions = [column["elements"][0] for column in group["columns"]]
        assert len(actions) == choice_count + 1
        assert [action["value"]["response"] for action in actions[:-1]] == choices
        assert actions[-1]["value"] == {
            "hermes_lark_action": "clarify_other",
            "clarify_id": "clarify-1",
        }
        assert all(action["width"] == "fill" for action in actions)

    def test_other_input_card_uses_real_feishu_form_actions(self) -> None:
        card = build_clarify_card(
            clarify_id="clarify-1",
            question="Which path?",
            choices=["A", "B"],
            status="input",
        )
        form = card["body"]["elements"][1]
        assert form["tag"] == "form"
        input_element, group = form["elements"]
        assert input_element["tag"] == "input"
        assert input_element["name"] == "clarify_other_input"
        assert group["tag"] == "column_set"
        assert group["flex_mode"] == "stretch"
        assert len(group["columns"]) == 2
        submit, back = [column["elements"][0] for column in group["columns"]]
        assert submit["name"] == "clarify_other_submit"
        assert submit["value"]["hermes_lark_action"] == "clarify_other_submit"
        assert submit["value"]["clarify_id"] == "clarify-1"
        assert submit["form_action_type"] == "submit"
        assert submit["width"] == "fill"
        assert back["name"] == "clarify_other_back"
        assert back["value"]["hermes_lark_action"] == "clarify_other_back"
        assert back["value"]["clarify_id"] == "clarify-1"
        assert back["form_action_type"] == "submit"
        assert back["width"] == "fill"

    @pytest.mark.parametrize("status", ["answered", "awaiting_text", "expired"])
    def test_non_pending_cards_remove_actions(self, status: str) -> None:
        card = build_clarify_card(
            clarify_id="clarify-1",
            question="Which path?",
            choices=["A", "B"],
            status=status,  # type: ignore[arg-type]
            answer="B" if status == "answered" else "",
        )

        assert all(element["tag"] not in {"button", "column_set", "form"} for element in card["body"]["elements"])


class TestBuildApprovalCard:
    @staticmethod
    def _buttons() -> list[dict]:
        return [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "value": {"hermes_action": action, "approval_id": 7, "future": action},
            }
            for action, label in (
                ("approve_once", "Allow Once"),
                ("approve_session", "Session"),
                ("approve_always", "Always"),
                ("deny", "Deny"),
            )
        ]

    def test_pending_preserves_every_official_payload(self) -> None:
        buttons = self._buttons()
        card = build_approval_card(
            command="git clean -fd",
            description="delete untracked files",
            buttons=buttons,
        )

        rows = [item for item in card["body"]["elements"] if item["tag"] == "column_set"]
        assert card["schema"] == "2.0"
        assert card["header"]["template"] == "orange"
        assert len(rows) == 1
        row = rows[0]
        assert row["flex_mode"] == "stretch"
        assert row["flex_mode"] != "flow"
        assert len(row["columns"]) == 4
        assert all(column["width"] == "weighted" and column["weight"] == 1 for column in row["columns"])
        assert [item["value"] for item in _official_buttons(card)] == [item["value"] for item in buttons]
        assert [item["value"]["hermes_action"] for item in _official_buttons(card)] == [
            "approve_once",
            "approve_session",
            "approve_always",
            "deny",
        ]
        assert all(item["tag"] == "button" for column in row["columns"] for item in column["elements"])
        assert all(item["width"] == "fill" for item in _official_buttons(card))
        assert all(column["horizontal_align"] == "center" for column in row["columns"])
        assert row["columns"][-1]["elements"][0]["value"]["hermes_action"] == "deny"

    def test_conditional_and_unknown_choices_are_not_invented_or_dropped(self) -> None:
        buttons = [self._buttons()[0], self._buttons()[-1]]
        buttons.insert(
            1,
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Future scope"},
                "value": {"hermes_action": "approve_future_scope", "approval_id": 7},
            },
        )
        card = build_approval_card(command="cmd", description="why", buttons=buttons)
        assert [item["value"] for item in _official_buttons(card)] == [item["value"] for item in buttons]
        rows = [item for item in card["body"]["elements"] if item["tag"] == "column_set"]
        assert len(rows) == 1
        assert len(rows[0]["columns"]) == len(buttons)
        assert rows[0]["flex_mode"] == "stretch"
        assert [column["elements"][0]["value"] for column in rows[0]["columns"]] == [
            item["value"] for item in buttons
        ]
        assert all(column["elements"][0]["width"] == "fill" for column in rows[0]["columns"])

    @pytest.mark.parametrize(
        ("status", "decision", "template", "needle"),
        [
            ("approved", "once", "green", "Allowed once"),
            ("approved", "session", "green", "Allowed for this session"),
            ("approved", "always", "green", "Always allowed"),
            ("denied", "deny", "red", "Denied"),
            ("expired", "", "grey", "no longer pending"),
        ],
    )
    def test_terminal_cards_have_no_actions(
        self, status: str, decision: str, template: str, needle: str
    ) -> None:
        card = build_approval_card(
            command="cmd",
            description="why",
            buttons=self._buttons(),
            status=status,  # type: ignore[arg-type]
            decision=decision,
        )
        assert card["header"]["template"] == template
        assert all(item["tag"] != "button" for item in card["body"]["elements"])
        assert needle in card["body"]["elements"][-1]["content"]


class TestStripInvalidImageKeys:
    def test_no_images_unchanged(self) -> None:
        assert _strip_invalid_image_keys("no images") == "no images"

    def test_img_prefix_kept(self) -> None:
        assert "img_v3_test" in _strip_invalid_image_keys("![a](img_v3_test)")

    def test_non_img_removed(self) -> None:
        assert "http://example.com/img.png" not in _strip_invalid_image_keys("![a](http://example.com/img.png)")


# --- 表格处理 ---


class TestFindTablesOutsideCodeBlocks:
    def test_no_tables(self) -> None:
        assert _find_tables_outside_code_blocks("no tables here") == []

    def test_single_table(self) -> None:
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        results = _find_tables_outside_code_blocks(text)
        assert len(results) == 1

    def test_table_inside_code_block_ignored(self) -> None:
        text = "```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
        assert _find_tables_outside_code_blocks(text) == []

    def test_mixed(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        text = f"{table}\n\n```\n{table}\n```"
        results = _find_tables_outside_code_blocks(text)
        assert len(results) == 1


class TestDowngradeTables:
    def test_within_limit_unchanged(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        text = f"{table}\n\n{table}\n\n{table}"
        assert _downgrade_tables(text) == text

    def test_over_limit_downgraded(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        text = "\n\n".join([table] * 6)
        result = _downgrade_tables(text)
        assert result.count("```") >= 2  # 超限表格被包装为代码块


# --- 文本拆分 ---


class TestSplitLongText:
    def test_short_text_not_split(self) -> None:
        assert _split_long_text("short") == ["short"]

    def test_long_text_split_at_paragraph(self) -> None:
        chunk = "x" * 1200
        text = f"{chunk}\n\n{chunk}\n\n{chunk}"
        parts = _split_long_text(text, limit=2000)
        assert len(parts) > 1

    def test_no_paragraph_break_falls_back_to_newline(self) -> None:
        lines = ["word " * 100 for _ in range(30)]
        text = "\n".join(lines)
        parts = _split_long_text(text, limit=500)
        assert len(parts) > 1

    def test_exact_limit_not_split(self) -> None:
        text = "a" * 2400
        assert len(_split_long_text(text)) == 1


# --- 工具面板 ---

_STEP_RUNNING = {
    "name": "read",
    "title": "Read",
    "status": "running",
    "detail": "",
    "output": "",
    "error": "",
    "icon": "icon",
    "elapsed_ms": 0,
    "result_block": None,
    "error_block": None,
}
_STEP_SUCCESS = {**_STEP_RUNNING, "status": "success", "output": "ok", "elapsed_ms": 100}


class TestBuildToolPanel:
    def test_empty_steps(self) -> None:
        panel = _build_tool_panel([])
        assert panel["element_id"] == TOOL_PANEL_ELEMENT_ID
        assert panel["header"]["title"]["tag"] == "plain_text"
        assert "Tool use" in panel["header"]["title"]["content"]
        assert panel["border"] == {"color": "grey", "corner_radius": "5px"}
        assert panel["padding"] == "8px 8px 8px 8px"

    def test_with_steps(self) -> None:
        panel = _build_tool_panel([_STEP_SUCCESS], elapsed_ms=500)
        assert panel["element_id"] == TOOL_PANEL_ELEMENT_ID

    def test_with_elapsed(self) -> None:
        panel = _build_tool_panel([_STEP_RUNNING], elapsed_ms=3000)
        title = panel["header"]["title"]["content"]
        assert "3.0s" not in title

    def test_header_reports_failed_steps_without_status_emoji(self) -> None:
        panel = _build_tool_panel([
            _STEP_SUCCESS,
            {**_STEP_RUNNING, "status": "error", "elapsed_ms": 1200},
        ])
        title = panel["header"]["title"]
        assert title["content"] == "Tool use · 2 steps · 1 failed"
        assert title["i18n_content"]["zh_cn"] == "工具执行 · 2 步 · 1 个失败"
        assert "🛠️" not in title["content"]

    def test_step_status_uses_duration_done_and_failed_labels(self) -> None:
        running = _build_tool_panel([_STEP_RUNNING])
        success = _build_tool_panel([_STEP_SUCCESS])
        failed = _build_tool_panel([{**_STEP_RUNNING, "status": "error", "elapsed_ms": 1200}])
        assert "Running" in str(running["elements"])
        assert "0.1s" in str(success["elements"])
        assert "Succeeded" not in str(success["elements"])
        assert "Failed" in str(failed["elements"])
        assert "1.2s" in str(failed["elements"])

    @pytest.mark.parametrize(
        ("status", "elapsed_ms", "icon_color", "text_color", "label"),
        [
            ("running", 0, "grey", "wathet", "Running"),
            ("success", 50, "grey", "green", "\uff1c0.1s"),
            ("success", 99, "grey", "green", "\uff1c0.1s"),
            ("success", 100, "grey", "green", "0.1s"),
            ("success", 200, "grey", "green", "0.2s"),
            ("success", 1200, "grey", "green", "1.2s"),
            ("success", 0, "grey", "green", "Done"),
            ("error", 50, "grey", "red", "Failed · \uff1c0.1s"),
            ("error", 99, "grey", "red", "Failed · \uff1c0.1s"),
            ("error", 100, "grey", "red", "Failed · 0.1s"),
            ("error", 200, "grey", "red", "Failed · 0.2s"),
            ("error", 1200, "grey", "red", "Failed · 1.2s"),
        ],
    )
    def test_step_status_colors_icon_and_status_text(
        self, status: str, elapsed_ms: int, icon_color: str, text_color: str, label: str
    ) -> None:
        title = _build_tool_step_title({**_STEP_RUNNING, "status": status, "elapsed_ms": elapsed_ms})

        assert title["icon"]["color"] == icon_color
        assert f"<font color='{text_color}'>{label}</font>" in title["text"]["content"]

    @pytest.mark.parametrize(
        ("elapsed_ms", "expected"),
        [(50, "\uff1c0.1s"), (99, "\uff1c0.1s"), (100, "0.1s"), (200, "0.2s"), (1200, "1.2s")],
    )
    def test_tool_elapsed_formatting_boundaries(self, elapsed_ms: int, expected: str) -> None:
        assert _format_tool_elapsed(elapsed_ms) == expected

    def test_detail_visibility_and_compact_mode_keep_title_and_output(self) -> None:
        step = {
            **_STEP_SUCCESS,
            "name": "exec",
            "title": "Run command",
            "detail": "python3 anysearch_cli.py batch_search --queries '[long payload]'",
            "error_block": {
                "language": "text",
                "content": "failure details",
                "fenced": "```text\nfailure details\n```",
            },
        }
        full = _build_tool_panel([step])
        compact = _build_tool_panel([step], tool_detail_mode="compact")
        hidden = _build_tool_panel([step], show_tool_detail=False)

        assert "python3 anysearch_cli.py batch_search --queries '[long payload]'" in str(full)
        assert "anysearch_cli.py batch_search" in str(compact)
        assert "--queries" not in str(compact)
        assert "failure details" in str(hidden)
        assert "python3 anysearch_cli.py" not in str(hidden)
        assert len(full["elements"]) == len(compact["elements"]) == len(hidden["elements"]) + 1

    def test_malformed_compact_detail_is_omitted_without_breaking_panel(self) -> None:
        step = {
            **_STEP_RUNNING,
            "name": "exec",
            "title": "Run command",
            "detail": 'python3 script.py --query "unfinished',
        }
        panel = _build_tool_panel([step], tool_detail_mode="compact")
        assert len(panel["elements"]) == 1
        assert "Run command" in str(panel["elements"][0])


# --- Footer ---


class TestBuildFooterElements:
    @staticmethod
    def _panel(result: list[dict]) -> dict:
        panel = result[1]
        assert panel["tag"] == "collapsible_panel"
        return panel

    @staticmethod
    def _unwrap_grey(value: str) -> str:
        return re.sub(r"<font color='grey'>(.*?)</font>", r"\1", value, flags=re.DOTALL)

    @staticmethod
    def _unwrap_summary_styles(value: str) -> str:
        value = re.sub(r"<font color='black'>(.*?)</font>", r"\1", value, flags=re.DOTALL)
        return re.sub(r"<font color='grey'>(.*?)</font>", r"\1", value, flags=re.DOTALL)

    @classmethod
    def _title(cls, result: list[dict]) -> dict:
        title = cls._panel(result)["header"]["title"].copy()
        title["content"] = cls._unwrap_summary_styles(title["content"])
        title["i18n_content"] = {
            locale: cls._unwrap_summary_styles(content)
            for locale, content in title["i18n_content"].items()
        }
        return title

    @classmethod
    def _content(cls, result: list[dict]) -> str:
        elements = cls._panel(result)["elements"]
        if not elements:
            return ""
        lines: list[str] = []
        for row in elements:
            if row.get("tag") == "markdown":
                lines.append(cls._unwrap_grey(row.get("content", "")))
            for column in row.get("columns", []):
                for element in column.get("elements", []):
                    if element.get("tag") == "markdown":
                        lines.append(cls._unwrap_grey(element.get("content", "")))
        return "\n".join(lines)

    @classmethod
    def _percentage_rows(cls, result: list[dict]) -> list[dict]:
        return [
            element
            for element in cls._panel(result)["elements"]
            if element.get("tag") == "column_set" and any(
                child.get("tag") == "chart"
                for column in element.get("columns", [])
                for child in column.get("elements", [])
            )
        ]

    @classmethod
    def _markdown_elements(cls, result: list[dict]) -> list[dict]:
        return [
            child
            for row in cls._panel(result)["elements"]
            for column in row.get("columns", [])
            for child in column.get("elements", [])
            if child.get("tag") == "markdown"
        ]

    def test_empty_data_renders_default_status(self) -> None:
        # 默认字段只包含额外 metadata；状态由 compact summary 展示。
        result = _build_footer_elements({})
        assert len(result) >= 2
        assert self._panel(result)["expanded"] is False
        assert self._title(result)["content"] == "✅"
        assert self._panel(result)["elements"] == []

    def test_summary_uses_elapsed_and_model(self) -> None:
        result = _build_footer_elements(
            {"duration": 26.5, "model": "gpt-5"},
            fields=[["status", "elapsed", "model"]],
        )
        title = self._title(result)
        assert title["content"] == "✅ 26.5s · gpt-5"
        assert title["i18n_content"]["zh_cn"] == "✅ 26.5s · gpt-5"
        assert "Run Details" not in title["content"]
        assert "运行详情" not in title["i18n_content"]["zh_cn"]

    def test_summary_uses_default_markdown_color(self) -> None:
        result = _build_footer_elements({"duration": 4.4, "model": "gpt-5.6-luna"})
        raw_title = self._panel(result)["header"]["title"]["content"]
        assert raw_title == "<font color='black'>✅ 4.4s · gpt-5.6-luna</font>"
        assert not raw_title.startswith("<font color='grey'>")

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({"model": "gpt-5"}, "✅ gpt-5"),
            ({"duration": 26.5}, "✅ 26.5s"),
            ({}, "✅"),
        ],
    )
    def test_summary_omits_missing_core_parts(self, data: dict, expected: str) -> None:
        result = _build_footer_elements(data, fields=[["status"]])
        assert self._title(result)["content"] == expected

    def test_gpt_quota_keeps_context_available_in_detail(self) -> None:
        remaining = "<font color='green'>80%</font>"
        reset_at = datetime.now().astimezone() + timedelta(days=5)
        result = _build_footer_elements(
            {
                "context_used": 50000,
                "context_max": 200000,
                "gpt_quota_remaining": remaining,
                "gpt_quota_reset_at": reset_at,
            },
            fields=[["status", "context", "gpt_quota", "quota_reset"]],
        )
        content = self._content(result)
        title = self._title(result)
        assert title["content"] == f"✅ {remaining}"
        assert "↻" not in content
        assert "GPT remaining 80%" in content
        assert "Context used 25%" in content

    def test_malformed_quota_reset_is_fail_open_for_detail(self) -> None:
        result = _build_footer_elements(
            {
                "gpt_quota_remaining": "<font color='green'>95%</font>",
                "gpt_quota_reset_at": "not-a-timestamp",
            },
            fields=[["quota_reset"]],
        )
        assert self._panel(result)["elements"] == []

    def test_quota_markup_is_rendered_in_markdown_summary_header(self) -> None:
        quota = "<font color='green'>95%</font>"
        result = _build_footer_elements(
            {
                "duration": 122,
                "model": "gpt-5.6-luna",
                "gpt_quota_remaining": quota,
                "gpt_quota_reset_at": "2026-08-16T02:30:00+00:00",
            },
            fields=[["gpt_quota", "quota_reset"]],
        )
        title = self._panel(result)["header"]["title"]
        assert title["tag"] == "markdown"
        assert title["content"] == (
            f"<font color='black'>✅ 2m 2s · gpt-5.6-luna · </font>{quota}"
        )
        assert quota in title["content"]
        assert f"<font color='grey'>{quota}" not in title["content"]
        assert not title["content"].startswith("<font color='grey'>")
        assert "GPT remaining 95%" in self._content(result)
        assert "↻" not in self._content(result)

    def test_balance_replaces_context_in_summary_without_quota(self) -> None:
        result = _build_footer_elements(
            {
                "duration": 122,
                "model": "deepseek-v3",
                "context_used": 50000,
                "context_max": 128000,
                "balance": "¥4.97",
            },
            fields=[["status", "elapsed", "model"]],
        )
        assert self._title(result)["content"] == "✅ 2m 2s · deepseek-v3 · ¥4.97"
        assert self._panel(result)["elements"] == []

    def test_status_error(self) -> None:
        result = _build_footer_elements({"input_tokens": 1}, is_error=True, fields=[["status", "tokens"]])
        assert self._title(result)["content"] == "❌ Error"
        assert self._content(result) == "<font color='red'>Tokens ↑ 1</font>"
        assert all(
            "<font color='grey'><font color='red'>" not in element["content"]
            for element in self._markdown_elements(result)
        )

    def test_status_aborted(self) -> None:
        result = _build_footer_elements({"output_tokens": 1}, is_aborted=True, fields=[["status", "tokens"]])
        assert self._title(result)["content"] == "⬛️ Stopped"
        assert self._content(result) == "Tokens ↓ 1"

    def test_explicit_stop_adds_continue_hint_to_grey_detail(self) -> None:
        result = _build_footer_elements(
            {"stop_continue_hint": True},
            is_aborted=True,
            fields=[["tokens"]],
        )
        panel = self._panel(result)
        assert self._content(result) == "You can continue this session."
        assert panel["elements"][0]["i18n_content"]["zh_cn"] == "<font color='grey'>会话仍可继续。</font>"
        assert "Stopped" not in self._content(result)

    def test_generic_aborted_has_no_continue_hint(self) -> None:
        result = _build_footer_elements({}, is_aborted=True, fields=[["tokens"]])
        assert self._panel(result)["elements"] == []

    def test_elapsed_displayed(self) -> None:
        result = _build_footer_elements({"duration": 12.5}, fields=[["elapsed"]])
        assert self._title(result)["content"] == "✅ 12.5s"
        assert self._content(result) == ""

    def test_model_displayed(self) -> None:
        result = _build_footer_elements({"model": "claude-3"}, fields=[["model"]])
        assert self._title(result)["content"] == "✅ claude-3"
        assert self._content(result) == ""

    def test_context_displayed(self) -> None:
        result = _build_footer_elements(
            {"context_used": 50000, "context_max": 200000},
            fields=[["context"]],
        )
        assert self._title(result)["content"] == "✅"
        assert "Context used 25%" in self._content(result)

    def test_tokens_displayed(self) -> None:
        result = _build_footer_elements(
            {"input_tokens": 1000, "output_tokens": 500},
            fields=[["tokens"]],
        )
        assert self._content(result) == "Tokens ↑ 1.0K · ↓ 500"

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({"input_tokens": 1200}, "Tokens ↑ 1.2K"),
            ({"output_tokens": 800}, "Tokens ↓ 800"),
        ],
    )
    def test_tokens_omit_missing_direction(self, data: dict, expected: str) -> None:
        result = _build_footer_elements(data, fields=[["tokens"]])
        assert self._content(result) == expected

    def test_tokens_with_no_usage_are_hidden(self) -> None:
        result = _build_footer_elements({}, fields=[["tokens"]])
        assert self._panel(result)["elements"] == []

    def test_cache_hit_uses_current_prompt_denominator(self) -> None:
        result = _build_footer_elements(
            {
                "input_tokens": 70_500,
                "cache_prompt_tokens": 70_500,
                "cache_read_tokens": 52_300,
            },
            fields=[["cache"]],
        )
        assert "Cache hit 74%" in self._content(result)
        assert "52.3K / 70.5K" in self._content(result)
        assert "缓存命中 74%" in str(self._panel(result)["elements"])

    @pytest.mark.parametrize(
        "data",
        [{}, {"cache_read_tokens": 0, "cache_write_tokens": 0}],
    )
    def test_cache_is_hidden_without_positive_usage(self, data: dict) -> None:
        result = _build_footer_elements(data, fields=[["cache"]])
        assert self._panel(result)["elements"] == []

    def test_cache_does_not_divide_by_zero(self) -> None:
        result = _build_footer_elements(
            {"cache_read_tokens": 1000, "cache_prompt_tokens": 0},
            fields=[["cache"]],
        )
        assert self._content(result) == "Cache Read 1.0K"

    def test_reasoning_tokens_are_optional(self) -> None:
        shown = _build_footer_elements(
            {"reasoning_tokens": 1600},
            fields=[["reasoning"]],
        )
        hidden = _build_footer_elements({}, fields=[["reasoning"]])
        assert self._content(shown) == "Reasoning 1.6K"
        assert self._panel(hidden)["elements"] == []

    def test_detail_fields_use_two_columns_and_share_footer_style(self) -> None:
        result = _build_footer_elements(
            {
                "input_tokens": 1000,
                "context_used": 500,
                "context_max": 2000,
                "balance": "¥4.97",
                "api_calls": 2,
            },
            fields=[["tokens", "context", "balance", "api_calls"]],
            text_size="normal_v2",
        )
        panel = self._panel(result)
        detail = panel["elements"][0]
        assert "Context used 25%" in self._content(result)
        assert self._title(result)["content"] == "✅ ¥4.97"
        assert "Balance ¥4.97" not in self._content(result)
        assert all(
            element.get("tag") != "markdown" or " · Context" not in element.get("content", "")
            for element in panel["elements"]
        )
        assert panel["header"]["title"]["text_size"] == "normal_v2"
        assert all(
            element.get("text_size") == "normal_v2"
            for element in panel["elements"]
            if element.get("tag") == "markdown"
        )
        assert not panel["header"]["title"]["content"].startswith("<font color='grey'>")
        assert any(
            element.get("content", "").startswith("<font color='grey'>")
            for element in self._markdown_elements(result)
        )
        detail_rows = [
            element
            for element in panel["elements"]
            if element.get("tag") == "column_set"
            and not any(
                child.get("tag") == "chart"
                for column in element.get("columns", [])
                for child in column.get("elements", [])
            )
        ]
        assert len(detail_rows[-1]["columns"]) == 2
        assert detail_rows[-1]["horizontal_spacing"] == "12px"
        assert "text_color" not in panel["header"]["title"]
        assert "text_color" not in detail

    def test_run_details_removes_border_but_keeps_padding(self) -> None:
        panel = self._panel(_build_footer_elements({"duration": 4.4, "model": "gpt-5"}))
        assert "border" not in panel
        assert panel["margin"] == "-6px 0px 0px 0px"
        assert panel["padding"] == "6px 0px 0px 0px"

    def test_default_gpt_details_keep_only_full_width_tokens_after_metrics(self) -> None:
        result = _build_footer_elements(
            {
                "gpt_quota_remaining": "<font color='green'>85%</font>",
                "gpt_quota_reset_at": "2026-08-18T00:42:00+00:00",
                "context_used": 19700,
                "context_max": 272000,
                "input_tokens": 19800,
                "output_tokens": 11,
                "cache_read_tokens": 18900,
                "cache_prompt_tokens": 19800,
            }
        )
        content = self._content(result)
        rows = self._percentage_rows(result)
        assert len(rows) == 1
        assert "GPT remaining 85%" in content
        assert "Context used 7%" in content
        assert "Tokens" in content
        assert "Cache hit" not in content
        assert "Reasoning" not in content
        assert "Reset " in content
        assert "Context 19.7K / 272.0K" not in content
        ordinary_rows = [
            element
            for element in self._panel(result)["elements"]
            if element.get("tag") == "column_set"
            and not any(
                child.get("tag") == "chart"
                for column in element.get("columns", [])
                for child in column.get("elements", [])
            )
        ]
        assert len(ordinary_rows) == 1
        assert len(ordinary_rows[0]["columns"]) == 1

    def test_context_and_cache_promote_without_provider_or_model_name(self) -> None:
        result = _build_footer_elements(
            {
                "context_used": 19700,
                "context_max": 272000,
                "input_tokens": 19800,
                "cache_read_tokens": 18900,
                "cache_prompt_tokens": 19800,
                "model": "any-runtime-name",
            }
        )
        rows = self._percentage_rows(result)
        assert len(rows) == 1
        content = self._content(result)
        assert "Context used 7%" in content
        assert "Cache hit 95%" in content
        assert "Context 19.7K / 272.0K" not in content

    def test_default_usage_billed_details_keep_only_full_width_tokens_after_metrics(self) -> None:
        result = _build_footer_elements(
            {
                "model": "deepseek-v3",
                "context_used": 19700,
                "context_max": 272000,
                "input_tokens": 19800,
                "output_tokens": 11,
                "cache_read_tokens": 18900,
                "cache_prompt_tokens": 19800,
                "reasoning_tokens": 3600,
                "balance": "¥4.97",
            },
        )
        assert self._title(result)["content"] == "✅ deepseek-v3 · ¥4.97"
        content = self._content(result)
        assert "Context used 7%" in content
        assert "Cache hit 95%" in content
        assert "Tokens" in content
        assert "Reasoning" not in content
        assert "Balance" not in content
        ordinary_rows = [
            element
            for element in self._panel(result)["elements"]
            if element.get("tag") == "column_set"
            and not any(
                child.get("tag") == "chart"
                for column in element.get("columns", [])
                for child in column.get("elements", [])
            )
        ]
        assert len(ordinary_rows) == 1
        assert len(ordinary_rows[0]["columns"]) == 1

    def test_only_context_builds_one_metric_without_empty_placeholder(self) -> None:
        result = _build_footer_elements({"context_used": 19700, "context_max": 272000})
        rows = self._percentage_rows(result)
        assert len(rows) == 1
        assert len(rows[0]["columns"]) == 2
        assert "Context used 7%" in self._content(result)

    def test_explicit_fields_do_not_promote_unconfigured_quota(self) -> None:
        result = _build_footer_elements(
            {
                "gpt_quota_remaining": "<font color='green'>85%</font>",
                "context_used": 19700,
                "context_max": 272000,
            },
            fields=[["context"]],
        )
        content = self._content(result)
        assert "Context used 7%" in content
        assert "GPT remaining" not in content

    def test_explicit_fields_keep_cache_and_reasoning_details(self) -> None:
        result = _build_footer_elements(
            {
                "gpt_quota_remaining": "<font color='green'>85%</font>",
                "context_used": 19700,
                "context_max": 272000,
                "input_tokens": 19800,
                "output_tokens": 11,
                "cache_read_tokens": 18900,
                "cache_prompt_tokens": 19800,
                "reasoning_tokens": 3600,
            },
            fields=[["tokens", "context", "gpt_quota", "cache", "reasoning"]],
        )
        content = self._content(result)
        assert "GPT remaining 85%" in content
        assert "Context used 7%" in content
        assert "Cache hit 95%" in content
        assert "Reasoning 3.6K" in content

    def test_runtime_default_fields_promote_quota_and_context(self) -> None:
        result = _build_footer_elements(
            {
                "gpt_quota_remaining": "<font color='green'>97%</font>",
                "gpt_quota_reset_at": "2026-08-18T00:42:00+00:00",
                "context_used": 19700,
                "context_max": 272000,
            },
            fields=[[
                "tokens", "context", "quota_reset", "cache", "reasoning", "balance"
            ]],
        )
        rows = self._percentage_rows(result)
        assert len(rows) == 1
        assert len(rows[0]["columns"]) == 4
        content = self._content(result)
        assert "GPT remaining 97%" in content
        assert "Context used 7%" in content
        assert "Reset " in content
        assert all(column["vertical_align"] == "center" for column in rows[0]["columns"])
        values = [
            column["elements"][0]["chart_spec"]["data"]["values"][0]["value"]
            for column in rows[0]["columns"]
            if column["elements"][0].get("tag") == "chart"
        ]
        assert values == [pytest.approx(0.97), pytest.approx(19700 / 272000)]

    def test_assistant_profile_default_fields_use_single_tokens_detail(self) -> None:
        result = _build_footer_elements(
            {
                "gpt_quota_remaining": "<font color='green'>97%</font>",
                "gpt_quota_reset_at": "2026-08-18T00:42:00+00:00",
                "context_used": 19800,
                "context_max": 272000,
                "input_tokens": 19800,
                "output_tokens": 11,
                "cache_read_tokens": 18900,
                "cache_prompt_tokens": 19800,
                "reasoning_tokens": 3600,
                "balance": "¥4.97",
            },
            fields=[[
                "status",
                "tokens",
                "context",
                "quota_reset",
                "cache",
                "reasoning",
                "balance",
            ]],
        )
        panel = self._panel(result)
        percentage_row = self._percentage_rows(result)[0]
        assert len(percentage_row["columns"]) == 4
        values = [
            column["elements"][0]["chart_spec"]["data"]["values"][0]["value"]
            for column in percentage_row["columns"]
            if column["elements"][0].get("tag") == "chart"
        ]
        assert values == [pytest.approx(0.97), pytest.approx(19800 / 272000)]
        content = self._content(result)
        assert "GPT remaining 97%" in content
        assert "Context used 7%" in content
        assert "Tokens" in content
        assert "Cache hit 95%" not in content
        assert "Reasoning 3.6K" not in content
        assert "Balance ¥4.97" not in content
        ordinary_rows = [
            element
            for element in panel["elements"]
            if element.get("tag") == "column_set"
            and not any(
                child.get("tag") == "chart"
                for column in element.get("columns", [])
                for child in column.get("elements", [])
            )
        ]
        assert [len(row["columns"]) for row in ordinary_rows] == [1]
        assert "Tokens" in str(ordinary_rows[0])

    def test_cache_without_valid_denominator_is_not_promoted(self) -> None:
        result = _build_footer_elements(
            {"cache_read_tokens": 18900, "cache_prompt_tokens": 0},
            fields=[["cache"]],
        )
        assert self._percentage_rows(result) == []
        assert "Cache Read 18.9K" in self._content(result)

    def test_quota_without_reset_still_builds_metric_without_empty_reset(self) -> None:
        result = _build_footer_elements(
            {"gpt_quota_remaining": "<font color='green'>85%</font>"},
            fields=[["gpt_quota", "quota_reset"]],
        )
        assert len(self._percentage_rows(result)) == 1
        content = self._content(result)
        assert "GPT remaining 85%" in content
        assert "Reset" not in content

    def test_percentage_chart_uses_safe_28px_non_highlighting_hover_spec(self) -> None:
        result = _build_footer_elements({"context_used": 19700, "context_max": 272000})
        row = self._percentage_rows(result)[0]
        circle_column = row["columns"][0]
        chart = circle_column["elements"][0]
        spec = chart["chart_spec"]
        assert circle_column["width"] == "28px"
        assert chart["height"] == "28px"
        assert chart["preview"] is False
        assert spec["type"] == "circularProgress"
        assert spec["data"]["values"] == [{"type": "Context", "value": pytest.approx(19700 / 272000)}]
        assert spec["categoryField"] == "type"
        assert spec["valueField"] == "value"
        assert spec["outerRadius"] == 0.81
        assert spec["innerRadius"] == 0.51
        assert spec["cornerRadius"] == 5
        assert spec["progress"]["style"]["fill"] == {
            "type": "threshold",
            "field": "value",
            "domain": [0.5, 0.8],
            "range": ["#A2C10B", "#FF811A", "#F54A45"],
        }
        assert spec["indicator"]["visible"] is False
        assert spec["legends"]["visible"] is False
        assert spec["hover"] is False
        expected_tooltip_pattern = {
            "title": {"value": "Context"},
            "content": [{"key": "Percentage", "value": "7%"}],
        }
        assert spec["tooltip"] == {
            "mark": expected_tooltip_pattern,
            "dimension": expected_tooltip_pattern,
        }
        assert spec["padding"] == 0
        assert "preview" not in spec
        assert "angleField" not in spec
        assert "colorField" not in spec
        text_column = row["columns"][1]
        assert circle_column["vertical_align"] == "center"
        assert text_column["vertical_align"] == "center"

    def test_percentage_chart_tooltip_uses_distinct_labels_and_rounded_percentages(self) -> None:
        result = _build_footer_elements(
            {
                "gpt_quota_remaining": "<font color='green'>92%</font>",
                "context_used": 57_116,
                "context_max": 272_000,
            },
        )
        charts = [
            column["elements"][0]["chart_spec"]
            for column in self._percentage_rows(result)[0]["columns"]
            if column["elements"][0].get("tag") == "chart"
        ]
        assert charts[0]["data"]["values"] == [{"type": "GPT quota", "value": 0.92}]
        assert charts[0]["tooltip"]["mark"] == {
            "title": {"value": "GPT quota"},
            "content": [{"key": "Percentage", "value": "92%"}],
        }
        assert charts[0]["tooltip"]["dimension"] == charts[0]["tooltip"]["mark"]
        assert charts[1]["data"]["values"] == [{"type": "Context", "value": pytest.approx(57_116 / 272_000)}]
        assert charts[1]["tooltip"]["mark"] == {
            "title": {"value": "Context"},
            "content": [{"key": "Percentage", "value": "21%"}],
        }
        assert charts[1]["tooltip"]["dimension"] == charts[1]["tooltip"]["mark"]

    @pytest.mark.parametrize(
        ("percentage", "expected"),
        [
            (100, "#A2C10B"),
            (50, "#A2C10B"),
            (49, "#FF811A"),
            (20, "#FF811A"),
            (19, "#F54A45"),
            (0, "#F54A45"),
        ],
    )
    def test_gpt_quota_circle_uses_remaining_thresholds(
        self,
        percentage: int,
        expected: str,
    ) -> None:
        assert _run_details_metric_color("gpt_quota", percentage / 100) == expected

    @pytest.mark.parametrize(
        ("percentage", "expected"),
        [
            (0, "#A2C10B"),
            (49, "#A2C10B"),
            (50, "#FF811A"),
            (79, "#FF811A"),
            (80, "#F54A45"),
            (100, "#F54A45"),
        ],
    )
    def test_context_circle_uses_used_risk_thresholds(
        self,
        percentage: int,
        expected: str,
    ) -> None:
        assert _run_details_metric_color("context", percentage / 100) == expected

    @pytest.mark.parametrize(
        ("percentage", "expected"),
        [(0, "#7AA2FF"), (79, "#7AA2FF"), (80, "#A2C10B"), (100, "#A2C10B")],
    )
    def test_cache_circle_uses_neutral_success_thresholds(
        self,
        percentage: int,
        expected: str,
    ) -> None:
        assert _run_details_metric_color("cache", percentage / 100) == expected

    @pytest.mark.parametrize(
        ("data", "expected_fraction", "expected_fill"),
        [
            (
                {"gpt_quota_remaining": "<font color='green'>97%</font>"},
                0.97,
                {
                    "type": "threshold",
                    "field": "value",
                    "domain": [0.2, 0.5],
                    "range": ["#F54A45", "#FF811A", "#A2C10B"],
                },
            ),
            (
                {
                    "cache_read_tokens": 52_300,
                    "cache_prompt_tokens": 70_500,
                },
                52_300 / 70_500,
                {
                    "type": "threshold",
                    "field": "value",
                    "domain": [0.8],
                    "range": ["#7AA2FF", "#A2C10B"],
                },
            ),
        ],
    )
    def test_percentage_chart_uses_metric_fraction_for_quota_and_cache(
        self,
        data: dict,
        expected_fraction: float,
        expected_fill: dict,
    ) -> None:
        fields = [["gpt_quota"]] if "gpt_quota_remaining" in data else [["cache"]]
        result = _build_footer_elements(data, fields=fields)
        chart = self._percentage_rows(result)[0]["columns"][0]["elements"][0]
        values = chart["chart_spec"]["data"]["values"]
        expected_type = "GPT quota" if "gpt_quota_remaining" in data else "Cache"
        expected_percentage = f"{round(expected_fraction * 100)}%"
        assert values == [{"type": expected_type, "value": pytest.approx(expected_fraction)}]
        assert chart["chart_spec"]["progress"]["style"]["fill"] == expected_fill
        assert chart["chart_spec"]["tooltip"] == {
            "mark": {
                "title": {"value": expected_type},
                "content": [{"key": "Percentage", "value": expected_percentage}],
            },
            "dimension": {
                "title": {"value": expected_type},
                "content": [{"key": "Percentage", "value": expected_percentage}],
            },
        }

    def test_default_tokens_row_is_full_width_when_cache_not_promoted(self) -> None:
        result = _build_footer_elements(
            {
                "gpt_quota_remaining": "<font color='green'>85%</font>",
                "context_used": 19700,
                "context_max": 272000,
                "input_tokens": 19800,
                "output_tokens": 11,
                "cache_read_tokens": 18900,
                "cache_prompt_tokens": 19800,
            }
        )
        rows = [
            element for element in self._panel(result)["elements"] if element.get("tag") == "column_set"
        ]
        assert len(rows) == 2
        ordinary = rows[1]
        assert len(ordinary["columns"]) == 1
        assert "horizontal_spacing" not in ordinary
        assert "Tokens" in str(ordinary)
        assert "Cache hit" not in str(ordinary)

    def test_show_label(self) -> None:
        result = _build_footer_elements(
            {"duration": 5},
            fields=[["elapsed"]],
            show_label=True,
        )
        assert self._content(result) == ""

    def test_multi_row_fields(self) -> None:
        result = _build_footer_elements(
            {"duration": 5, "model": "gpt"},
            fields=[["elapsed"], ["model"]],
        )
        assert self._content(result) == ""

    def test_field_subset_only_renders_selected_fields(self) -> None:
        result = _build_footer_elements(
            {"duration": 5, "model": "gpt", "balance": "$2"},
            fields=[["model"]],
        )
        content = self._content(result)
        assert content == ""

    def test_summary_is_independent_from_footer_fields(self) -> None:
        result = _build_footer_elements(
            {
                "duration": 122,
                "model": "gpt-5.6-luna",
                "gpt_quota_remaining": "<font color='green'>96%</font>",
                "input_tokens": 12400,
                "output_tokens": 2100,
            },
            fields=[["tokens"]],
        )
        assert self._title(result)["content"] == (
            "✅ 2m 2s · gpt-5.6-luna · <font color='green'>96%</font>"
        )
        assert self._content(result) == "Tokens ↑ 12.4K · ↓ 2.1K"

    def test_reset_timestamp_formats_local_absolute_time_and_cross_year(self) -> None:
        local_now = datetime.now().astimezone().replace(second=0, microsecond=0)
        same_year = local_now.replace(month=8, day=16, hour=10, minute=30)
        en, zh = _format_quota_reset_at(same_year, now=local_now)
        assert en == "Aug 16, 10:30"
        assert zh == "8月16日 10:30"

        next_year = same_year.replace(year=local_now.year + 1)
        en_next, zh_next = _format_quota_reset_at(next_year, now=local_now)
        assert str(local_now.year + 1) in en_next
        assert str(local_now.year + 1) in zh_next

    def test_reset_timestamp_converts_utc_to_local_timezone(self) -> None:
        local_now = datetime.now().astimezone().replace(second=0, microsecond=0)
        reset_utc = local_now.astimezone(UTC)
        en, zh = _format_quota_reset_at(reset_utc, now=local_now)
        assert en is not None and zh is not None
        assert local_now.strftime("%H:%M") in en
        assert local_now.strftime("%H:%M") in zh

    def test_default_details_keep_only_primary_run_usage(self) -> None:
        reset_at = datetime.now().astimezone() + timedelta(days=5)
        result = _build_footer_elements(
            {
                "duration": 4.4,
                "model": "gpt-5.6-luna",
                "gpt_quota_remaining": "<font color='green'>95%</font>",
                "gpt_quota_reset_at": reset_at,
                "input_tokens": 12400,
                "output_tokens": 1800,
                "context_used": 70500,
                "context_max": 272000,
                "cache_read_tokens": 10200,
                "cache_write_tokens": 1100,
                "cache_prompt_tokens": 12400,
                "reasoning_tokens": 3600,
                "balance": "¥4.97",
            }
        )
        content = self._content(result)
        assert "GPT remaining 95%" in content
        assert "Context used 26%" in content
        assert "Tokens" in content
        assert "Cache hit 82%" not in content
        assert "Reasoning 3.6K" not in content
        assert "Balance ¥4.97" not in content
        assert "Status" not in content
        assert "Elapsed" not in content
        assert "Model" not in content
        assert "GPT Quota" not in content
        assert any(
            element.get("content", "").startswith("<font color='grey'>")
            for element in self._markdown_elements(result)
        )
        assert all(
            element.get("text_size") == "notation"
            for element in self._panel(result)["elements"]
            if element.get("tag") == "markdown"
        )

    def test_no_matching_fields(self) -> None:
        result = _build_footer_elements({}, fields=[["tokens"]])
        assert self._title(result)["content"] == "✅"
        assert self._panel(result)["elements"] == []

    def test_empty_fields_preserve_existing_no_matching_semantics(self) -> None:
        result = _build_footer_elements({"model": "gpt"}, fields=[])
        assert self._title(result)["content"] == "✅ gpt"
        assert self._panel(result)["elements"] == []

    def test_run_details_failure_falls_back_to_legacy_footer(self) -> None:
        with patch(
            "hermes_lark_streaming.cardkit.builder._build_run_details_elements",
            side_effect=RuntimeError("presentation failure"),
        ):
            result = _build_footer_elements({})
        assert result[1]["content"] == "✅"


# --- 推理面板 ---


class TestBuildReasoningPanel:
    def test_without_elapsed(self) -> None:
        panel = _build_reasoning_panel("thinking content")
        assert "Thought" in panel["header"]["title"]["content"]
        assert not panel["expanded"]

    def test_with_elapsed(self) -> None:
        panel = _build_reasoning_panel("thoughts", elapsed_ms=5000)
        title = panel["header"]["title"]["content"]
        assert "5.0s" in title

    def test_expanded_true(self) -> None:
        panel = _build_reasoning_panel("text", expanded=True)
        assert panel["expanded"] is True

    def test_element_id_default_none(self) -> None:
        panel = _build_reasoning_panel("text")
        assert "element_id" not in panel

    def test_inner_markdown_has_element_id(self) -> None:
        panel = _build_reasoning_panel("text")
        inner = panel["elements"][0]
        assert inner["element_id"] == REASONING_TEXT_ELEMENT_ID

    def test_title_is_plain_text_grey(self) -> None:
        panel = _build_reasoning_panel("text")
        title = panel["header"]["title"]
        assert title["tag"] == "plain_text"
        assert title["text_color"] == "grey"
        assert title["text_size"] == "notation"
        assert panel["border"] == {"color": "grey", "corner_radius": "5px"}
        assert panel["padding"] == "8px 8px 8px 8px"

    def test_empty_text_shows_thinking_title(self) -> None:
        panel = _build_reasoning_panel(" ")
        assert "Thinking" in panel["header"]["title"]["content"]

    def test_with_content_shows_thought_title(self) -> None:
        panel = _build_reasoning_panel("reasoning here")
        assert "Thought" in panel["header"]["title"]["content"]
        assert "Thinking" not in panel["header"]["title"]["content"]


# --- 数字格式化 ---


class TestCompact:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (42, "42"),
            (1_500, "1.5K"),
            (2_500_000, "2.5M"),
            (1_000, "1.0K"),
            (250_000_000, "250M"),
        ],
    )
    def test_compacts_numbers(self, value: int, expected: str) -> None:
        assert _compact(value) == expected


class TestFormatElapsed:
    @pytest.mark.parametrize(
        ("milliseconds", "expected"),
        [(3_500, "3.5s"), (125_000, "2m 5s"), (60_000, "1m 0s")],
    )
    def test_formats_elapsed_time(self, milliseconds: float, expected: str) -> None:
        assert _format_elapsed(milliseconds) == expected


# --- 工具函数 ---


class TestEscapeMd:
    def test_escapes_special_chars(self) -> None:
        result = _escape_md("a`b*c{d}e[f]g<h>i")
        assert "\\" in result

    def test_plain_text_unchanged(self) -> None:
        assert _escape_md("hello world") == "hello world"


class TestLongestBacktickRun:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("no backticks", 0), ("a `b` c", 1), ("```code```", 3)],
    )
    def test_finds_longest_run(self, text: str, expected: int) -> None:
        assert _longest_backtick_run(text) == expected


# --- 完整卡片构建 ---


class TestBuildStreamingCardV2:
    def test_structure(self) -> None:
        card = build_streaming_card_v2()
        assert card["schema"] == "2.0"
        assert card["config"]["streaming_mode"] is True
        assert card["body"]["elements"]
        assert not any(
            "Run Details" in element.get("header", {}).get("title", {}).get("content", "")
            for element in card["body"]["elements"]
        )

    def test_with_tool_steps(self) -> None:
        card = build_streaming_card_v2(tool_steps=[_STEP_RUNNING], elapsed_ms=100)
        assert any(e.get("element_id") == TOOL_PANEL_ELEMENT_ID for e in card["body"]["elements"])

    def test_streaming_card_keeps_native_loading_icon(self) -> None:
        elements = build_streaming_card_v2()["body"]["elements"]
        loading = elements[-1]

        assert loading["element_id"] == _LOADING_ELEMENT_ID
        assert loading["content"] == " "
        assert loading["icon"] == {
            "tag": "custom_icon",
            "img_key": "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg",
            "size": "16px 16px",
        }
        assert not any(element.get("element_id") == "progress_status" for element in elements)

    def test_card_mode_before_heartbeat_is_still_native_loading_icon(self) -> None:
        elements = build_streaming_card_v2(progress_snapshot=ProgressState().snapshot())["body"]["elements"]
        loading = elements[-1]

        assert loading["element_id"] == _LOADING_ELEMENT_ID
        assert loading["content"] == " "
        assert "i18n_content" not in loading
        assert not any(element.get("element_id") == "progress_status" for element in elements)

    def test_heartbeat_updates_loading_content_without_replacing_icon(self) -> None:
        state = ProgressState()
        state.note_heartbeat(180, iteration=3, max_iterations=60)
        elements = build_streaming_card_v2(progress_snapshot=state.snapshot())["body"]["elements"]
        loading = elements[-1]

        assert loading["element_id"] == _LOADING_ELEMENT_ID
        assert loading["content"] == "Working · 3 min · Round 3"
        assert loading["i18n_content"]["zh_cn"] == "运行中 · 3 分钟 · 第 3 轮"
        assert loading["icon"]["img_key"] == "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg"
        assert not any(element.get("element_id") == "progress_status" for element in elements)

    def test_no_tool_use(self) -> None:
        card = build_streaming_card_v2(show_tool_use=False)
        assert not any(e.get("element_id") == TOOL_PANEL_ELEMENT_ID for e in card["body"]["elements"])

    def test_show_reasoning_adds_panel(self) -> None:
        card = build_streaming_card_v2(show_reasoning=True)
        assert any(e.get("element_id") == REASONING_ELEMENT_ID for e in card["body"]["elements"])

    def test_show_reasoning_default_no_panel(self) -> None:
        card = build_streaming_card_v2()
        assert not any(e.get("element_id") == REASONING_ELEMENT_ID for e in card["body"]["elements"])

    def test_reasoning_before_tool_before_answer(self) -> None:
        card = build_streaming_card_v2(
            show_reasoning=True,
            tool_steps=[_STEP_RUNNING],
            elapsed_ms=100,
            show_tool_use=True,
        )
        ids = [e.get("element_id") for e in card["body"]["elements"]]
        reasoning_idx = ids.index(REASONING_ELEMENT_ID)
        tool_idx = ids.index(TOOL_PANEL_ELEMENT_ID)
        assert reasoning_idx < tool_idx

    def test_width_mode_default(self) -> None:
        card = build_streaming_card_v2()
        assert card["config"]["width_mode"] == "default"

    def test_width_mode_custom(self) -> None:
        card = build_streaming_card_v2(width_mode="compact")
        assert card["config"]["width_mode"] == "compact"


# --- 分段完成态卡片 ---


def _seg(seg_type: str, text: str = "", **kwargs: int | float) -> Segment:
    """创建测试用 Segment mock."""
    seg = Segment(seg_type, f"{seg_type}_0")
    seg.text = text
    if seg_type == "reasoning":
        seg.text_el_id = f"{seg_type}_0_text"
    seg.tool_offset = int(kwargs.get("tool_offset", 0))
    seg.tool_end_offset = int(kwargs.get("tool_end_offset", 0))
    seg.elapsed_ms = float(kwargs.get("elapsed_ms", 0.0))
    seg.start_time = float(kwargs.get("start_time", 0.0))
    seg.created = True
    seg.dirty = False
    return seg


class TestBuildSegmentCompleteCard:
    def test_empty_segments_and_skipped_reasoning(self) -> None:
        """空 segments 渲染 Done；空 reasoning 被跳过."""
        card = build_complete_card(segments=[], all_tool_steps=[])
        assert card["schema"] == "2.0"
        assert any("Done" in str(e) or "完成" in str(e) for e in card["body"]["elements"])

        card2 = build_complete_card(segments=[_seg("reasoning", "")], all_tool_steps=[])
        assert any("Done" in str(e) or "完成" in str(e) for e in card2["body"]["elements"])

    def test_answer_only_no_done(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hello world")],
            all_tool_steps=[],
        )
        elements = card["body"]["elements"]
        assert any("hello world" in str(e) for e in elements)
        assert not any("Done" in str(e) for e in elements)

    def test_reasoning_before_answer(self) -> None:
        card = build_complete_card(
            segments=[_seg("reasoning", "think"), _seg("answer", "reply")],
            all_tool_steps=[],
        )
        contents = [str(e) for e in card["body"]["elements"]]
        r_idx = next(i for i, c in enumerate(contents) if "think" in c)
        a_idx = next(i for i, c in enumerate(contents) if "reply" in c)
        assert r_idx < a_idx

    def test_merged_reasoning_renders_one_final_panel(self) -> None:
        card = build_complete_card(
            segments=[
                _seg("reasoning", "R1"),
                _seg("tool", tool_offset=0, tool_end_offset=1),
                _seg("reasoning", "R2"),
                _seg("tool", tool_offset=1, tool_end_offset=2),
                _seg("reasoning", "R3"),
                _seg("answer", "answer"),
            ],
            all_tool_steps=[_STEP_SUCCESS, _STEP_RUNNING],
            merged_reasoning_text="R1R2R3",
            merged_reasoning_elapsed_ms=6000,
            show_tool_use=False,
        )

        reasoning_panels = [
            element
            for element in card["body"]["elements"]
            if element.get("tag") == "collapsible_panel"
            and "💭" in element.get("header", {}).get("title", {}).get("content", "")
        ]
        assert len(reasoning_panels) == 1
        assert reasoning_panels[0]["elements"][0]["content"] == "R1R2R3"
        assert "6.0s" in reasoning_panels[0]["header"]["title"]["content"]
        assert reasoning_panels[0]["expanded"] is False

    def test_segmented_reasoning_still_renders_each_panel(self) -> None:
        card = build_complete_card(
            segments=[
                _seg("reasoning", "R1"),
                _seg("tool", tool_offset=0, tool_end_offset=1),
                _seg("reasoning", "R2"),
                _seg("answer", "answer"),
            ],
            all_tool_steps=[_STEP_SUCCESS],
        )
        reasoning_panels = [
            element
            for element in card["body"]["elements"]
            if element.get("tag") == "collapsible_panel"
            and "💭" in element.get("header", {}).get("title", {}).get("content", "")
        ]
        assert len(reasoning_panels) == 2

    def test_tool_segment_uses_steps_slice(self) -> None:
        steps = [_STEP_RUNNING, _STEP_SUCCESS, _STEP_RUNNING]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=1, tool_end_offset=3)],
            all_tool_steps=steps,
        )
        tool_elements = [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]
        tool_elements = [e for e in tool_elements if e.get("element_id") == TOOL_PANEL_ELEMENT_ID]
        assert len(tool_elements) == 1
        assert len(tool_elements[0].get("elements", [])) == 2  # steps[1:3]

    @pytest.mark.parametrize("panel_expanded", [False, True])
    def test_run_details_stays_collapsed_independent_of_panel_expanded(
        self, panel_expanded: bool
    ) -> None:
        card = build_complete_card(
            segments=[_seg("reasoning", "think"), _seg("answer", "reply")],
            all_tool_steps=[],
            footer_data={"duration": 26.5, "model": "gpt-5"},
            panel_expanded=panel_expanded,
        )
        details = next(
            element
            for element in card["body"]["elements"]
            if element.get("tag") == "collapsible_panel"
            and "💭" not in element.get("header", {}).get("title", {}).get("content", "")
            and element.get("element_id") is None
        )
        reasoning = next(
            element
            for element in card["body"]["elements"]
            if element.get("tag") == "collapsible_panel"
            and "💭" in element.get("header", {}).get("title", {}).get("content", "")
        )
        assert details["expanded"] is False
        assert reasoning["expanded"] is panel_expanded

    def test_tool_visibility_does_not_hide_run_details(self) -> None:
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=0, tool_end_offset=1), _seg("answer", "hello")],
            all_tool_steps=[_STEP_SUCCESS],
            show_tool_use=False,
            footer_data={"duration": 1.0, "model": "gpt-5"},
        )
        assert not any(e.get("element_id") == TOOL_PANEL_ELEMENT_ID for e in card["body"]["elements"])
        assert any(
            e.get("tag") == "collapsible_panel"
            and "💭" not in e.get("header", {}).get("title", {}).get("content", "")
            and e.get("element_id") is None
            for e in card["body"]["elements"]
        )

    def test_multiple_tool_segments_render_one_unified_panel(self) -> None:
        card = build_complete_card(
            segments=[
                _seg("reasoning", "plan"),
                _seg("tool", tool_offset=0, tool_end_offset=1),
                _seg("answer", "interim"),
                _seg("tool", tool_offset=1, tool_end_offset=2),
                _seg("answer", "final"),
            ],
            all_tool_steps=[_STEP_SUCCESS, _STEP_RUNNING],
        )
        panels = [
            element for element in card["body"]["elements"]
            if element.get("element_id") == TOOL_PANEL_ELEMENT_ID
        ]
        assert len(panels) == 1
        assert len(panels[0]["elements"]) == 2
        assert panels[0]["expanded"] is False

    def test_panel_expanded_override_is_respected_in_complete_card(self) -> None:
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=0, tool_end_offset=1)],
            all_tool_steps=[_STEP_SUCCESS],
            panel_expanded=True,
        )
        panel = next(
            element for element in card["body"]["elements"]
            if element.get("element_id") == TOOL_PANEL_ELEMENT_ID
        )
        assert panel["expanded"] is True

    def test_three_round_ordering(self) -> None:
        card = build_complete_card(
            segments=[
                _seg("reasoning", "r1"),
                _seg("answer", "a1"),
                _seg("tool", tool_offset=0, tool_end_offset=2),
                _seg("reasoning", "r2"),
                _seg("answer", "a2"),
            ],
            all_tool_steps=[_STEP_SUCCESS, _STEP_RUNNING],
        )
        contents = [str(e) for e in card["body"]["elements"]]
        r1 = next(i for i, c in enumerate(contents) if "r1" in c)
        a1 = next(i for i, c in enumerate(contents) if "a1" in c)
        r2 = next(i for i, c in enumerate(contents) if "r2" in c)
        a2 = next(i for i, c in enumerate(contents) if "a2" in c)
        assert r1 < a1 < r2 < a2

    def test_tool_end_offset_zero_uses_all_steps(self) -> None:
        steps = [_STEP_SUCCESS, _STEP_RUNNING]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=0, tool_end_offset=0)],
            all_tool_steps=steps,
        )
        inner = next(e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel")["elements"]
        assert len(inner) == 2

    def test_complete_card_width_mode_default(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
        )
        assert card["config"]["width_mode"] == "default"

    def test_complete_card_width_mode_custom(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            width_mode="fill",
        )
        assert card["config"]["width_mode"] == "fill"

    def test_tool_empty_steps_skipped(self) -> None:
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=5, tool_end_offset=5)],
            all_tool_steps=[_STEP_SUCCESS],
        )
        assert not any(e.get("element_id") == TOOL_PANEL_ELEMENT_ID for e in card["body"]["elements"])

    def test_show_tool_use_false_hides_tool_panel(self) -> None:
        """show_tool_use=False → TOOL segment rendered as nothing (无工具面板)."""
        steps = [_STEP_RUNNING, _STEP_SUCCESS]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=0, tool_end_offset=2), _seg("answer", "hello")],
            all_tool_steps=steps,
            show_tool_use=False,
        )
        # 无 collapsible_panel（工具面板）
        assert not any(e.get("element_id") == TOOL_PANEL_ELEMENT_ID for e in card["body"]["elements"])
        # 但 answer 依然在
        assert any(e.get("tag") == "markdown" and "hello" in str(e.get("content", ""))
                   for e in card["body"]["elements"])

    def test_show_tool_use_true_default_shows_panel(self) -> None:
        """show_tool_use 默认 True → 工具面板保留（向后兼容）."""
        steps = [_STEP_RUNNING, _STEP_SUCCESS]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=0, tool_end_offset=2)],
            all_tool_steps=steps,
        )
        assert any(e.get("tag") == "collapsible_panel" for e in card["body"]["elements"])

    def test_summary_truncated_from_last_answer(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "short"), _seg("answer", "x" * 200)],
            all_tool_steps=[],
        )
        summary = card["config"].get("summary", {}).get("content", "")
        assert len(summary) <= 120


class TestBuildCronCard:
    def test_basic_card_structure(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card("Hello **world**")
        assert card["schema"] == "2.0"
        assert card["body"]["elements"][0]["tag"] == "markdown"
        assert "Hello **world**" in card["body"]["elements"][0]["content"]

    def test_summary_from_content(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card("Line 1\nLine 2\n" + "x" * 200)
        summary = card["config"]["summary"]["content"]
        assert summary.startswith("Line 1 Line 2")
        assert len(summary) <= 120

    def test_empty_content(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card("")
        assert card["body"]["elements"] == []

    def test_table_content_preserved(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        content = "| A | B |\n|---|---|\n| 1 | 2 |"
        card = build_cron_card(content)
        assert "| A | B |" in card["body"]["elements"][0]["content"]

    def test_header_with_task_name(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card("Hello", task_name="daily-digest")
        assert card["header"]["title"]["content"] == ":Alarm: daily-digest"
        assert card["header"]["title"]["tag"] == "lark_md"
        assert card["header"]["template"] == "blue"

    def test_no_header_without_task_name(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card("Hello")
        assert "header" not in card

    def test_header_with_task_name_and_run_time(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card(
            "Hello",
            task_name="daily-digest",
            run_time="2026-06-10T14:30:00+08:00",
        )
        assert card["header"]["title"]["content"] == ":Alarm: daily-digest · 2026-06-10 14:30"

    def test_header_with_run_time_only(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card("Hello", run_time="2026-06-10T14:30:00+08:00")
        assert card["header"]["title"]["content"] == ":Alarm: 2026-06-10 14:30"

    def test_header_invalid_run_time_falls_back_to_raw(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card("Hello", run_time="not-a-date")
        assert card["header"]["title"]["content"] == ":Alarm: not-a-date"

    def test_header_run_time_without_timezone(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card("Hello", run_time="2026-06-10T14:30:00")
        assert card["header"]["title"]["content"] == ":Alarm: 2026-06-10 14:30"


# --- Header ---


class TestBuildHeader:
    @pytest.mark.parametrize(
        ("status", "template", "title"),
        [
            ("streaming", "blue", "Processing"),
            ("completed", "green", "Completed"),
            ("error", "red", "Error"),
            ("stopped", "red", "Stopped"),
            ("unknown", "green", "Completed"),
        ],
    )
    def test_status_header(self, status: str, template: str, title: str) -> None:
        header = _build_header(status)
        assert header is not None
        assert header["template"] == template
        assert title in header["title"]["content"]

    def test_title_has_i18n(self) -> None:
        header = _build_header("streaming")
        assert "i18n_content" in header["title"]
        assert "zh_cn" in header["title"]["i18n_content"]
        assert "en_us" in header["title"]["i18n_content"]

class TestStreamingCardHeader:
    def test_header_absent_by_default(self) -> None:
        card = build_streaming_card_v2()
        assert "header" not in card

    def test_header_present_when_enabled(self) -> None:
        card = build_streaming_card_v2(header_enabled=True)
        assert "header" in card
        assert card["header"]["template"] == "blue"


class TestCompleteCardHeader:
    def test_completed_has_green_header(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            header_enabled=True,
        )
        assert "header" in card
        assert card["header"]["template"] == "green"

    def test_aborted_has_red_header(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            is_aborted=True,
            header_enabled=True,
        )
        assert "header" in card
        assert card["header"]["template"] == "red"

    def test_error_has_red_header(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            is_error=True,
            header_enabled=True,
        )
        assert "header" in card
        assert card["header"]["template"] == "red"
        assert "Error" in card["header"]["title"]["content"]

    def test_header_disabled(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            header_enabled=False,
        )
        assert "header" not in card


class TestCompleteCardFooter:
    def test_footer_present_by_default(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
        )
        tags = [e.get("tag") for e in card["body"]["elements"]]
        assert "hr" in tags
        details = next(
            e
            for e in card["body"]["elements"]
            if e.get("tag") == "collapsible_panel"
            and "💭" not in e.get("header", {}).get("title", {}).get("content", "")
            and e.get("element_id") is None
        )
        assert details["expanded"] is False

    def test_footer_disabled(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            footer_enabled=False,
        )
        tags = [e.get("tag") for e in card["body"]["elements"]]
        assert "hr" not in tags
        assert not any(
            e.get("tag") == "collapsible_panel"
            and "💭" not in e.get("header", {}).get("title", {}).get("content", "")
            and e.get("element_id") is None
            for e in card["body"]["elements"]
        )
