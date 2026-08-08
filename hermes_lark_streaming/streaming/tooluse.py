"""工具调用追踪与可视化."""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypedDict

from .progress import ActivityKind


class ToolStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


class ToolBlock(TypedDict):
    language: str
    content: str
    fenced: str


class ToolDisplayStep(TypedDict):
    name: str
    title: str
    status: str
    detail: str
    output: str
    error: str
    icon: str
    elapsed_ms: float
    result_block: ToolBlock | None
    error_block: ToolBlock | None


@dataclass
class ToolStep:
    name: str
    status: ToolStatus
    detail: str = ""
    output: str = ""
    error: str = ""
    result_block: ToolBlock | None = None
    error_block: ToolBlock | None = None
    started_at: float = 0.0
    elapsed_ms: float = 0.0


@dataclass
class ToolSession:
    steps: list[ToolStep] = field(default_factory=list)
    started_at: float = 0.0


_SENSITIVE_NAME_RE = re.compile(
    r"token|secret|password|api[_-]?key|authorization|cookie|credential"
    r"|bearer|session[_-]?id|client[_-]?secret|access[_-]?key",
    re.IGNORECASE,
)

_INLINE_ASSIGNMENT_RE = re.compile(r'(^|[\s"\'`])([A-Za-z_][A-Za-z0-9_]*)(=(?:"[^"]*"|\'[^\']*\'|[^\s"\'`]+))')
_AUTH_HEADER_RE = re.compile(
    r"(Authorization\s*:\s*(?:Bearer|Basic|Token)\s+)([^\'\"\s]+)",
    re.IGNORECASE,
)
_SECRET_FLAG_RE = re.compile(
    r'((?:^|[\s"\'`]))(--?[A-Za-z0-9][A-Za-z0-9-]*)(=|\s+)("(?:[^"]*)"|\'(?:[^\']*)\'|[^\s"\'`]+)'
)


def redact_inline_secrets(value: str) -> str:
    """脱敏 key=secret、Authorization header、--flag secret 模式."""

    def _redact_assign(m: re.Match) -> str:
        key = str(m.group(2))
        if _SENSITIVE_NAME_RE.search(key):
            return f"{m.group(1)}{key}=[redacted]"
        return str(m.group(0))

    def _redact_flag(m: re.Match) -> str:
        flag = re.sub(r"^-+", "", str(m.group(2)))
        if _SENSITIVE_NAME_RE.search(flag):
            return f"{m.group(1)}{m.group(2)}{m.group(3)}[redacted]"
        return str(m.group(0))

    return _SECRET_FLAG_RE.sub(
        _redact_flag,
        _AUTH_HEADER_RE.sub(r"\1[redacted]", _INLINE_ASSIGNMENT_RE.sub(_redact_assign, value)),
    )


def _sanitize_detail(text: str, sanitizer: str | None) -> str:
    """根据 sanitizer 类型清洗 detail 文本."""
    if not text or not sanitizer:
        return text
    cleaned = re.sub(r"<[^>]+>", "", text).strip()
    if not cleaned:
        return text
    if sanitizer == "command":
        cleaned = redact_inline_secrets(cleaned)
        return _redact_paths(cleaned)
    if sanitizer == "path":
        return _basename_only(re.sub(r"^(?:from|file|path)\s+", "", cleaned, flags=re.IGNORECASE).strip())
    if sanitizer == "search":
        return cleaned.strip("'\"")
    if sanitizer == "url":
        if cleaned.lower().startswith("from "):
            return cleaned.strip("'\"").replace("from ", "", 1)
        return cleaned.strip("'\"")
    return cleaned


def _redact_paths(text: str) -> str:
    """命令中路径只保留 basename."""
    return re.sub(
        r'(^|[\s=\'"()])([~./][^\s\'"()]+)',
        lambda m: f"{m.group(1)}{os.path.basename(m.group(2))}",
        text,
    )


def _basename_only(text: str) -> str:
    if not text:
        return text
    return os.path.basename(text.replace("\\", "/").rstrip("/"))


_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SAFE_COMMAND_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")
_PYTHON_MODULE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_SHELL_OPERATORS = frozenset({"&&", "||", "|", ";", ">", ">>", "<", "<<"})
_PYTHON_FLAG_OPTIONS = frozenset(
    {"-B", "-d", "-E", "-i", "-I", "-O", "-OO", "-P", "-q", "-s", "-S", "-u", "-v", "-V", "-x"}
)
_PYTHON_VALUE_OPTIONS = frozenset({"-W", "-X", "--check-hash-based-pycs"})


def _safe_command_token(token: str) -> bool:
    """Whether a token is conservative enough to be a displayed subcommand."""
    return bool(
        _SAFE_COMMAND_TOKEN_RE.fullmatch(token)
        and "/" not in token
        and "\\" not in token
        and "=" not in token
    )


def _command_unit(tokens: list[str]) -> list[str]:
    """Return only the first shell command unit, without redirect payloads."""
    for index, token in enumerate(tokens):
        if token in _SHELL_OPERATORS or token.startswith(">") or token.startswith("<"):
            return tokens[:index]
    return tokens


def _executable_basename(token: str) -> str:
    return os.path.basename(token.replace("\\", "/").rstrip("/"))


def _append_safe_subcommand(result: list[str], tokens: list[str], index: int) -> None:
    if index < len(tokens) and _safe_command_token(tokens[index]):
        result.append(tokens[index])


def _is_shell_command_string_option(token: str) -> bool:
    """Return whether a shell short-option token includes the ``c`` flag."""
    return token.startswith("-") and not token.startswith("--") and "c" in token[1:]


def _compact_python_command(tokens: list[str]) -> str:
    """Compact Python argv while keeping interpreter options separate from script argv."""
    executable = tokens[0]
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-m":
            if index + 1 >= len(tokens):
                return executable
            module = tokens[index + 1]
            if not _PYTHON_MODULE_RE.fullmatch(module):
                return executable
            return f"{executable} -m {module}"
        if token == "-c":
            return executable
        if token == "--":
            index += 1
            break
        if token in _PYTHON_FLAG_OPTIONS:
            index += 1
            continue
        if token in _PYTHON_VALUE_OPTIONS:
            if index + 1 >= len(tokens):
                return executable
            index += 2
            continue
        if token.startswith("-"):
            return executable
        result = [_executable_basename(token)]
        _append_safe_subcommand(result, tokens, index + 1)
        return " ".join(result)
    if index < len(tokens):
        result = [_executable_basename(tokens[index])]
        _append_safe_subcommand(result, tokens, index + 1)
        return " ".join(result)
    return executable


def compact_command_detail(detail: str) -> str:
    """Semantically compact a sanitized command without exposing its payload.

    This function intentionally accepts only the already sanitized detail
    string stored on ``ToolDisplayStep``. Parsing failures fail closed so a
    malformed command cannot break card rendering or reveal a long payload.
    """
    if not detail.strip():
        return ""
    try:
        tokens = _command_unit(shlex.split(detail))
    except ValueError:
        return ""
    while tokens and _ENV_ASSIGNMENT_RE.fullmatch(tokens[0]):
        tokens.pop(0)
    if not tokens:
        return ""

    executable = _executable_basename(tokens[0])
    executable_lower = executable.lower()
    result: list[str]
    if executable_lower in {"python", "python3"} or re.fullmatch(r"python3\.\d+", executable_lower):
        return _compact_python_command(tokens)

    if executable_lower in {"bash", "sh", "zsh"}:
        if any(_is_shell_command_string_option(option) for option in tokens[1:]):
            return executable
        if "--command" in tokens[1:]:
            return executable
        script_index = next(
            (index for index, token in enumerate(tokens[1:], 1) if not token.startswith("-")),
            None,
        )
        if script_index is None:
            return executable
        result = [_executable_basename(tokens[script_index])]
        _append_safe_subcommand(result, tokens, script_index + 1)
        return " ".join(result)

    if executable_lower == "node":
        if any(option in tokens[1:] for option in {"-e", "--eval"}):
            return executable
        script_index = next(
            (index for index, token in enumerate(tokens[1:], 1) if not token.startswith("-")),
            None,
        )
        if script_index is None:
            return executable
        result = [_executable_basename(tokens[script_index])]
        _append_safe_subcommand(result, tokens, script_index + 1)
        return " ".join(result)

    if executable_lower == "git":
        option_with_value = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
        subcommand = None
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token in option_with_value:
                index += 2
                continue
            if not token.startswith("-"):
                subcommand = token
                break
            index += 1
        return " ".join([executable, subcommand]) if subcommand else executable

    result = [executable]
    _append_safe_subcommand(result, tokens, 1)
    return " ".join(result)


def compact_tool_detail(step: ToolDisplayStep) -> str:
    """Return compact presentation derived solely from sanitized step detail."""
    descriptor = _resolve_tool_descriptor(step.get("name"))
    if not descriptor or descriptor.get("sanitizer") != "command":
        return step.get("detail", "")
    return compact_command_detail(step.get("detail", ""))


def tool_detail_for_display(
    step: ToolDisplayStep,
    *,
    show_detail: bool = True,
    mode: str = "full",
) -> str:
    """Apply the current detail visibility/mode to a sanitized display step."""
    if not show_detail:
        return ""
    detail = step.get("detail", "").strip()
    if not detail:
        return ""
    if mode == "compact":
        return compact_tool_detail(step)
    return detail


_TOOL_DESCRIPTORS: list[dict[str, Any]] = [
    {"aliases": ["skill"], "icon": "app-default_outlined", "title": "Load skill", "sanitizer": None},
    {
        "aliases": ["read", "open"],
        "activity": ActivityKind.READING,
        "icon": "file-link-text_outlined",
        "title": "Read",
        "sanitizer": "path",
        "no_result": True,
    },
    {
        "aliases": ["write", "edit"],
        "icon": "edit_outlined",
        "title": "Edit",
        "sanitizer": "path",
        "no_result": True,
    },
    {
        "aliases": ["web_search", "web-search", "search"],
        "activity": ActivityKind.SEARCHING,
        "icon": "search_outlined",
        "title": "Search",
        "sanitizer": "search",
    },
    {
        "aliases": ["web_fetch", "web-fetch", "fetch"],
        "activity": ActivityKind.READING,
        "icon": "language_outlined",
        "title": "Fetch web page",
        "sanitizer": "url",
        "no_result": True,
    },
    {
        "aliases": ["grep"],
        "activity": ActivityKind.SEARCHING,
        "icon": "doc-search_outlined",
        "title": "Search text",
        "sanitizer": "search",
    },
    {
        "aliases": ["glob"],
        "activity": ActivityKind.SEARCHING,
        "icon": "folder_outlined",
        "title": "Search files",
        "sanitizer": "path",
    },
    {
        "aliases": ["exec", "bash", "command", "run"],
        "activity": ActivityKind.EXECUTING_COMMAND,
        "activity_aliases": ["shell", "python"],
        "icon": "setting_outlined",
        "title": "Run command",
        "sanitizer": "command",
    },
    {
        "aliases": ["terminal"],
        "activity": ActivityKind.EXECUTING_COMMAND,
        "icon": "setting-inter_outlined",
        "title": "Terminal",
        "sanitizer": "command",
    },
    {
        "aliases": ["browser", "playwright", "navigate"],
        "activity": ActivityKind.SEARCHING,
        "icon": "browser-mac_outlined",
        "title": "Browser",
        "no_result": True,
    },
    {"aliases": ["agent", "task", "spawn"], "icon": "robot_outlined", "title": "Run sub-agent"},
    {"aliases": ["check", "determine", "verify"], "icon": "list-check_outlined", "title": "Check"},
    {"aliases": ["summarize", "analyze", "prepare"], "icon": "report_outlined", "title": "Analyze"},
    {"aliases": ["clarify"], "icon": "chat_outlined", "title": "Clarify", "no_result": True},
]


def _resolve_tool_descriptor(name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    normalized = name.strip().lower().replace("-", "_")
    for desc in _TOOL_DESCRIPTORS:
        for alias in desc["aliases"]:
            if normalized == alias or normalized.startswith(f"{alias}_"):
                return desc
    return None


def activity_for_tool(name: str | None) -> ActivityKind:
    """Map a structured tool name through the shared Tool Panel registry."""
    if not name:
        return ActivityKind.USING_TOOL
    normalized = name.strip().lower().replace("-", "_")
    for desc in _TOOL_DESCRIPTORS:
        aliases = [*desc["aliases"], *desc.get("activity_aliases", [])]
        for alias in aliases:
            normalized_alias = alias.replace("-", "_")
            if normalized == normalized_alias or normalized.startswith(f"{normalized_alias}_"):
                activity = desc.get("activity")
                return activity if isinstance(activity, ActivityKind) else ActivityKind.USING_TOOL
    return ActivityKind.USING_TOOL


def _humanize_tool_name(name: str) -> str:
    cleaned = name.replace("-", " ").replace("_", " ").strip()
    if not cleaned:
        return "Tool"
    return cleaned[0].upper() + cleaned[1:]


def _format_duration_label(ms: float) -> str:
    return f"{ms:.0f} ms" if ms < 1000 else f"{(ms / 1000):.1f} s"


def _build_display_block(
    value: Any,
    fallback_lang: str = "json",
    *,
    sanitizer: str | None = None,
) -> ToolBlock | None:
    """构建结果/错误的显示块 — 返回 {language, content, fenced} 含 markdown 代码围栏."""
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.replace("\r\n", "\n").strip()
        if not normalized:
            return None
        if sanitizer == "command":
            normalized = redact_inline_secrets(normalized)
        if normalized.startswith("{") or normalized.startswith("["):
            try:
                parsed = json.loads(normalized)
                pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
                return _fenced_block("json", pretty)
            except json.JSONDecodeError:
                pass
        return _fenced_block("text" if fallback_lang == "json" else fallback_lang, normalized)
    if isinstance(value, (dict, list)):
        try:
            return _fenced_block("json", json.dumps(value, ensure_ascii=False, indent=2))
        except (TypeError, ValueError):
            pass
    normalized = str(value).strip()
    return _fenced_block("text", normalized) if normalized else None


def _fenced_block(language: str, content: str) -> ToolBlock:
    fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", content)), default=0) + 1)
    return {"language": language, "content": content, "fenced": f"{fence}{language}\n{content}\n{fence}"}


class ToolUseTracker:
    """追踪当前消息中的工具调用步骤.

    按 session 隔离，每个会话独立生命周期.
    """

    def __init__(self, max_steps: int = 128) -> None:
        self._session: ToolSession | None = None
        self._max_steps = max_steps

    @property
    def elapsed_ms(self) -> float:
        if self._session is None:
            return 0.0
        return (time.time() - self._session.started_at) * 1000

    @property
    def has_running(self) -> bool:
        """Whether at least one real Hermes tool call is still running."""
        return bool(
            self._session
            and any(step.status == ToolStatus.RUNNING for step in self._session.steps)
        )

    @property
    def active_activity(self) -> ActivityKind | None:
        """Activity for the most recently started tool that is still running."""
        if self._session is None:
            return None
        for step in reversed(self._session.steps):
            if step.status == ToolStatus.RUNNING:
                return activity_for_tool(step.name)
        return None

    @property
    def step_count(self) -> int:
        return len(self._session.steps) if self._session else 0

    @property
    def error_count(self) -> int:
        if self._session is None:
            return 0
        return sum(step.status == ToolStatus.ERROR for step in self._session.steps)

    def record_start(self, name: str, detail: str = "") -> None:
        if self._session is None:
            self._session = ToolSession(started_at=time.time())
        if len(self._session.steps) >= self._max_steps:
            return
        self._session.steps.append(
            ToolStep(
                name=name,
                status=ToolStatus.RUNNING,
                detail=detail,
                started_at=time.time(),
            )
        )

    def record_end(self, name: str, *, error: str = "", output: str = "") -> None:
        """通过名字匹配最近的一个 running 步骤来结束."""
        if self._session is None:
            return
        desc = _resolve_tool_descriptor(name)
        sanitizer = desc.get("sanitizer") if desc else None
        for step in reversed(self._session.steps):
            if step.name == name and step.status == ToolStatus.RUNNING:
                step.status = ToolStatus.ERROR if error else ToolStatus.SUCCESS
                step.error = error
                step.output = output
                step.elapsed_ms = (time.time() - step.started_at) * 1000
                if error:
                    step.error_block = _build_display_block(error, "text", sanitizer=sanitizer)
                elif output:
                    step.result_block = _build_display_block(output, "json", sanitizer=sanitizer)
                return
        self._session.steps.append(
            ToolStep(
                name=name,
                status=ToolStatus.ERROR if error else ToolStatus.SUCCESS,
                detail=error or output,
                output=output,
                error=error,
                started_at=time.time(),
                error_block=_build_display_block(error, "text", sanitizer=sanitizer) if error else None,
                result_block=_build_display_block(output, "json", sanitizer=sanitizer) if output else None,
            )
        )

    def build_display_steps(self) -> list[ToolDisplayStep]:
        """构建用于卡片渲染的步骤列表."""
        if self._session is None:
            return []
        steps: list[ToolDisplayStep] = []
        for s in self._session.steps:
            desc = _resolve_tool_descriptor(s.name)
            base_title = desc["title"] if desc else _humanize_tool_name(s.name)
            sanitizer = desc.get("sanitizer") if desc else None
            detail = _sanitize_detail(s.detail, sanitizer)
            steps.append(
                {
                    "name": s.name,
                    "title": base_title,
                    "status": s.status.value,
                    "detail": detail,
                    "output": s.output,
                    "error": s.error,
                    "icon": desc["icon"] if desc else "setting-inter_outlined",
                    "elapsed_ms": s.elapsed_ms,
                    "result_block": None if (desc and desc.get("no_result")) else s.result_block,
                    "error_block": s.error_block,
                }
            )
        return steps
