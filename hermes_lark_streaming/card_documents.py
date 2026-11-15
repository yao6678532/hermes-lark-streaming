"""Lossless byte-budget projection of the existing card JSON, without new UI."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

CARD_BYTE_BUDGET = 27_000


def serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def elements(value: Any) -> Iterator[tuple[list[Any], int, dict[str, Any]]]:
    if isinstance(value, dict):
        for child in value.values():
            yield from elements(child)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, dict):
                if child.get("element_id"):
                    yield value, index, child
                yield from elements(child)


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def split_markdown(text: str) -> tuple[str, str]:
    """Prefer paragraph/block boundaries; reopen oversized fenced code verbatim."""
    middle = len(text) // 2
    boundaries = [m.end() for m in re.finditer(r"\n\n|\n", text) if middle // 2 <= m.end() <= middle]
    cut = boundaries[-1] if boundaries else middle
    fence = None
    safe = []
    offset = 0
    for line in text[:cut].splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)", line)
        if match:
            marker, info = match.groups()
            if fence is None:
                fence = (marker, info.rstrip())
            elif marker[0] == fence[0][0] and len(marker) >= len(fence[0]) and not info.strip():
                fence = None
        offset += len(line)
        if offset > cut:
            # A cut in a huge code line still belongs to the currently open fence.
            break
        if fence is None and middle // 2 <= offset <= middle:
            safe.append(offset)
    if safe:
        cut = safe[-1]
        fence = None
    if fence is not None:
        marker, info = fence
        return text[:cut] + "\n" + marker, marker + info + "\n" + text[cut:]
    return text[:cut], text[cut:]


def apply_actions(card: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    result = deepcopy(card)
    for action in actions:
        kind, params = action["action"], action["params"]
        if kind == "partial_update_setting":
            settings = params.get("settings", params)
            for key, value in settings.items():
                if isinstance(value, dict):
                    result.setdefault(key, {}).update(deepcopy(value))
                else:
                    result[key] = deepcopy(value)
            continue
        if kind == "delete_elements":
            ids = set(params["element_ids"])
            for parent, index, child in reversed(list(elements(result))):
                if child["element_id"] in ids:
                    del parent[index]
            continue
        target = params.get("target_element_id") if kind == "add_elements" else params.get("element_id")
        found = next((x for x in elements(result) if x[2]["element_id"] == target), None)
        if found is None:
            raise ValueError(f"Card document has no element {target}")
        parent, index, node = found
        if kind == "add_elements":
            inserted = deepcopy(params["elements"])
            current_ids = {x[2]["element_id"] for x in elements(result)}
            if any(x.get("element_id") in current_ids for x in inserted):
                raise ValueError("Duplicate logical card element ID")
            if params.get("type") in ("append", "append_to"):
                node.setdefault("elements", []).extend(inserted)
            else:
                index += params.get("type") == "insert_after"
                parent[index:index] = inserted
        elif kind == "partial_update_element":
            _merge(node, params["partial_element"])
        elif kind == "update_element":
            parent[index] = {"element_id": target, **deepcopy(params["element"])}
        else:
            raise ValueError(f"Unsupported card document action: {kind}")
    return result


def replace_content(card: dict[str, Any], element_id: str, content: str) -> dict[str, Any]:
    return apply_actions(
        card,
        [
            {
                "action": "partial_update_element",
                "params": {
                    "element_id": element_id,
                    "partial_element": {"content": content},
                },
            }
        ],
    )


def _split_node(node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Divide rendered content, preserving its original wrappers and styling."""
    for key in ("elements", "columns"):
        children = node.get(key)
        if isinstance(children, list) and children:
            left, right = deepcopy(node), deepcopy(node)
            if len(children) > 1:
                middle = len(children) // 2
                left[key], right[key] = deepcopy(children[:middle]), deepcopy(children[middle:])
            else:
                a, b = _split_node(children[0])
                left[key], right[key] = [a], [b]
            return left, right
    # Tool titles/output live inside text dictionaries, rather than body elements.
    for key in ("text",):
        if isinstance(node.get(key), dict):
            a, b = _split_node(node[key])
            left, right = deepcopy(node), deepcopy(node)
            left[key], right[key] = a, b
            return left, right
    text = node.get("content")
    if not isinstance(text, str) or len(text) < 2:
        raise ValueError("An indivisible card element exceeds the byte budget")
    left, right = deepcopy(node), deepcopy(node)
    left["content"], right["content"] = split_markdown(text)
    for language, translated in node.get("i18n_content", {}).items():
        left["i18n_content"][language], right["i18n_content"][language] = split_markdown(translated)
    return left, right


def needs_partition(card: dict[str, Any]) -> bool:
    reserve = 2048 if card.get("config", {}).get("streaming_mode") else 0
    return serialized_size(card) > CARD_BYTE_BUDGET - reserve


def partition_card(card: dict[str, Any], *, budget: int = CARD_BYTE_BUDGET) -> list[dict[str, Any]]:
    """A small card is byte-for-byte unchanged; overflow retains every body node."""
    # Leave room for the existing terminal status/usage footer during streaming.
    content_budget = budget - 2048 if card.get("config", {}).get("streaming_mode") else budget
    if serialized_size(card) <= content_budget:
        return [deepcopy(card)]
    base = deepcopy(card)
    body = base.setdefault("body", {})
    nodes = body.pop("elements", [])
    if serialized_size(base) >= budget:
        raise ValueError("Card configuration exceeds the byte budget")
    # Status, loading indicator and Run Details belong on the final physical card.
    tail_index = next(
        (
            i
            for i, n in enumerate(nodes)
            if n.get("element_id")
            in {
                "run_details_divider",
                "loading_icon",
            }
        ),
        len(nodes),
    )
    queue = list(nodes[:tail_index])
    tail = list(nodes[tail_index:])
    chunks: list[list[dict[str, Any]]] = [[]]
    while queue:
        node = queue.pop(0)
        probe = deepcopy(base)
        probe["body"]["elements"] = [*chunks[-1], node]
        if serialized_size(probe) <= content_budget:
            chunks[-1].append(node)
        elif chunks[-1]:
            chunks.append([])
            queue.insert(0, node)
        else:
            left, right = _split_node(node)
            queue[0:0] = [left, right]
    # The terminal footer is atomic: its divider and panel stay on the last card.
    probe = deepcopy(base)
    probe["body"]["elements"] = [*chunks[-1], *tail]
    if serialized_size(probe) > budget:
        chunks.append([])
    chunks[-1].extend(tail)
    result = []
    for index, chunk in enumerate(chunks):
        part = deepcopy(base)
        part["body"]["elements"] = chunk
        if index != len(chunks) - 1:
            part.setdefault("config", {})["streaming_mode"] = False
        if serialized_size(part) > budget:
            raise ValueError("Card footer exceeds the byte budget")
        result.append(part)
    return result


@dataclass
class CardDocument:
    card: dict[str, Any]
    parts: list[str]
    messages: list[str | None] = field(default_factory=lambda: [None])
    chat_id: str = ""
    anchor_id: str | None = None
    partitioned: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    views: list[dict[str, Any]] = field(default_factory=list)
    pending: tuple[str, str] | None = None
    write_uncertain: bool = False
    closed: bool = False
    unconfirmed: bool = False
    delivery_ids: list[str] = field(default_factory=lambda: [uuid.uuid4().hex])


async def publish_document(
    client: Any, doc: CardDocument, candidate: dict[str, Any], *, shutdown: bool = False
) -> None:
    views = partition_card(candidate)
    doc.partitioned = True
    if not doc.anchor_id and not doc.chat_id:
        raise ValueError("Card rollover has no confirmed delivery route")
    for index, view in enumerate(views):
        if index < len(doc.parts):
            # A sealed prefix may become the only part after a corrected snapshot.
            # Keep using full-card snapshots; never reactivate its streaming lease.
            if not client.mutations.state(doc.parts[index]).streaming:
                view.setdefault("config", {})["streaming_mode"] = False
            if index < len(doc.views) and doc.views[index] == view:
                continue
            ident = doc.parts[index]
            if client.mutations.state(ident).streaming and not view.get("config", {}).get("streaming_mode"):
                await client._raw_close(ident, shutdown=shutdown)
            await client._raw_update(ident, view, shutdown=shutdown)
        else:
            ident = await client._raw_create(view)
            # Preserve this identity before sending: retries must not create a new entity.
            doc.parts.append(ident)
            doc.messages.append(None)
            doc.delivery_ids.append(uuid.uuid4().hex)
            root_state = client.mutations.state(doc.parts[0])
            child_state = client.mutations.state(ident)
            child_state.owner, child_state.pin = root_state.owner, root_state.pin
        if doc.messages[index] is None:
            delivered = await client.send_card_id_to_chat(
                doc.chat_id,
                ident,
                reply_to_message_id=doc.anchor_id,
                operation_id=doc.delivery_ids[index],
            )
            if delivered.startswith("hls-pending:"):
                from .feishu import DeliveryUncertain

                raise DeliveryUncertain("Card reference delivery is not confirmed")
            doc.messages[index] = delivered
        if index < len(doc.views):
            doc.views[index] = deepcopy(view)
        else:
            doc.views.append(deepcopy(view))
    while len(doc.parts) > len(views):
        await client._delete_owned_message(doc.messages[-1])
        client.mutations.state(doc.parts[-1]).available = False
        doc.parts.pop()
        doc.messages.pop()
        doc.delivery_ids.pop()
        if len(doc.views) > len(views):
            doc.views.pop()
    doc.card = deepcopy(candidate)
    doc.partitioned = True
