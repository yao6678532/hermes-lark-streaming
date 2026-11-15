"""飞书 Open API 客户端 — 基于 lark-oapi SDK."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import httpx
import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    BatchUpdateCardRequest,
    BatchUpdateCardRequestBody,
    Card,
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .card_documents import (
    CardDocument,
    apply_actions,
    needs_partition,
    partition_card,
    publish_document,
    replace_content,
)
from .card_mutations import WRITE_GUARD, CardMutations
from .config import DEFAULT_DOMAIN

_logger = logging.getLogger("hermes_lark_streaming")

_OPEN_APIS_SUFFIX = "/open-apis"

CARDKIT_GATEWAY_TIMEOUT = 2200
CARDKIT_INTERNAL_ERROR = 1663
CARDKIT_SERVER_INTERNAL_ERROR = 300000
CARDKIT_TRANSIENT_ERROR_CODES = frozenset(
    {
        CARDKIT_GATEWAY_TIMEOUT,
        CARDKIT_INTERNAL_ERROR,
        CARDKIT_SERVER_INTERNAL_ERROR,
    }
)
NETWORK_ERRORS = (TimeoutError, OSError, httpx.RequestError)
_TRANSIENT_RETRY_DELAYS_SEC = (0.15, 0.5, 1.0)


def _sanitize_message(msg: str) -> str:
    """从错误消息中移除 token 和 secret."""
    msg = re.sub(r'(tenant_access_token["\s:=]+)([A-Za-z0-9_-]{10,})', r"\1***", msg)
    msg = re.sub(r'(app_secret["\s:=]+)([A-Za-z0-9]{10,})', r"\1***", msg)
    msg = re.sub(r"(Bearer\s+)([A-Za-z0-9_-]{10,})", r"\1***", msg)
    return msg


class FeishuAPIError(RuntimeError):
    """飞书 API 错误，携带 API 错误码."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code

    def extract_sub_code(self) -> int | None:
        """从 msg 字符串中提取子错误码.

        格式: "Failed to create card content, ext=ErrCode: 11310; ..."
        """
        m = re.search(r"ErrCode:\s*(\d+)", str(self))
        if m:
            return int(m.group(1))
        return None


class DeliveryUncertain(FeishuAPIError):
    """A deduplicated message write has no acknowledgement; never send a fallback."""


CARDKIT_RATE_LIMITED = 230020  # 频控
CARDKIT_CONTENT_FAILED = 230099  # 卡片内容创建失败（通用码，需检查子错误）
CARDKIT_ELEMENT_NOT_FOUND = 300313  # CardKit element 尚未可见或不存在
CARDKIT_ELEMENT_LIMIT = 11310  # 子码: 卡片元素数量超限
CARDKIT_STREAMING_CLOSED = 300309  # 卡片流式模式已关闭
MSG_NOT_FOUND = 1000023  # 消息不存在/已删除


@dataclass(frozen=True)
class FeishuClientConfig:
    app_id: str
    app_secret: str
    base_url: str = DEFAULT_DOMAIN

    def __post_init__(self) -> None:
        if not isinstance(self.app_id, str) or not self.app_id.strip():
            raise ValueError("app_id is required")
        if not isinstance(self.app_secret, str) or not self.app_secret.strip():
            raise ValueError("app_secret is required")


class FeishuClient:
    """飞书 REST API 封装 — 基于 lark-oapi SDK.

    SDK 自动管理 tenant_access_token 的获取和刷新.
    """

    def __init__(self, config: FeishuClientConfig, *, mutations: CardMutations | None = None) -> None:
        self.config = config
        self._mutations = mutations if mutations is not None else CardMutations()
        domain = config.base_url.strip().rstrip("/").removesuffix(_OPEN_APIS_SUFFIX) or DEFAULT_DOMAIN
        builder = lark.Client.builder().app_id(config.app_id).app_secret(config.app_secret).domain(domain)
        self._client = builder.build()

    @staticmethod
    def _check(response: Any, operation: str) -> None:
        """检查 SDK 响应，失败时抛出 FeishuAPIError."""
        if not response.success():
            code = response.code or 0
            msg = response.msg or ""
            raise FeishuAPIError(
                _sanitize_message(f"{operation}: code={code}, msg={msg}"),
                code,
            )

    @staticmethod
    def _dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False)

    async def _checked_call(self, operation: str, call: Any) -> Any:
        """Run a Feishu SDK call and retry transient CardKit/Lark server errors."""
        attempts = len(_TRANSIENT_RETRY_DELAYS_SEC) + 1
        last_error: FeishuAPIError | None = None
        for attempt in range(attempts):
            if guard := WRITE_GUARD.get():
                guard()
            else:
                self.mutations.check_writes()
            try:
                resp = await call()
            except NETWORK_ERRORS as error:
                if guard is None or operation == "cardkit_batch_update":
                    raise
                if attempt >= attempts - 1:
                    raise DeliveryUncertain(f"{operation}: write acknowledgement unavailable") from error
                await asyncio.sleep(_TRANSIENT_RETRY_DELAYS_SEC[attempt])
                continue
            try:
                self._check(resp, operation)
                return resp
            except FeishuAPIError as exc:
                last_error = exc
                if exc.code not in CARDKIT_TRANSIENT_ERROR_CODES or attempt >= attempts - 1:
                    raise
                delay = _TRANSIENT_RETRY_DELAYS_SEC[attempt]
                _logger.warning(
                    "%s transient Feishu API error code=%s, retrying attempt=%d/%d delay=%.2fs",
                    operation,
                    exc.code,
                    attempt + 2,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    async def _message_call(self, operation: str, call: Any) -> Any:
        # The request object (including UUID) is immutable across confirmation attempts.
        for attempt in range(4):
            try:
                return await self._checked_call(operation, call)
            except NETWORK_ERRORS as error:
                if attempt == 3:
                    raise DeliveryUncertain(f"{operation}: acknowledgement unavailable") from error
                await asyncio.sleep(_TRANSIENT_RETRY_DELAYS_SEC[attempt])
            except FeishuAPIError as error:
                if error.code in CARDKIT_TRANSIENT_ERROR_CODES:
                    raise DeliveryUncertain(f"{operation}: server result unavailable") from error
                raise
        raise AssertionError("unreachable")

    async def send_card_to_chat(
        self,
        chat_id: str,
        card: dict[str, Any],
        *,
        reply_to_message_id: str | None = None,
    ) -> str:
        # Reference cards have no body, and are already partitioned by CardDocument.
        parts = partition_card(card) if "body" in card else [card]
        delivered = ""
        try:
            for part in parts:
                ident = await self._send_static_card(chat_id, part, reply_to_message_id=reply_to_message_id)
                delivered = delivered or ident
        except Exception as error:
            if delivered:
                raise DeliveryUncertain("Multipart card delivery is incomplete") from error
            raise
        return delivered

    async def _send_static_card(
        self,
        chat_id: str,
        card: dict[str, Any],
        *,
        reply_to_message_id: str | None = None,
    ) -> str:
        """发送独立卡片到聊天（非回复），返回 message_id."""
        request_uuid = uuid.uuid4().hex
        if reply_to_message_id:
            request = (
                ReplyMessageRequest.builder()
                .message_id(reply_to_message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(self._dumps(card))
                    .uuid(request_uuid)
                    .build()
                )
                .build()
            )
            resp = await self._message_call(
                "send_card_to_chat",
                lambda: self._client.im.v1.message.areply(request),
            )
        else:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(self._dumps(card))
                    .uuid(request_uuid)
                    .build()
                )
                .build()
            )
            resp = await self._message_call(
                "send_card_to_chat",
                lambda: self._client.im.v1.message.acreate(request),
            )
        if resp.data and resp.data.message_id:
            return str(resp.data.message_id)
        raise FeishuAPIError("send_card_to_chat: response missing message_id")

    async def send_text_to_chat(self, chat_id: str, text: str, *, operation_id: str | None = None) -> str:
        """Send a plain-text fail-open hint without entering Hermes Gateway."""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(self._dumps({"text": str(text)}))
                .uuid(operation_id or uuid.uuid4().hex)
                .build()
            )
            .build()
        )
        resp = await self._message_call(
            "send_text_to_chat",
            lambda: self._client.im.v1.message.acreate(request),
        )
        if resp.data and resp.data.message_id:
            return str(resp.data.message_id)
        raise FeishuAPIError("send_text_to_chat: response missing message_id")

    async def reply_card_by_id(
        self,
        message_id: str,
        card_id: str,
        *,
        operation_id: str | None = None,
    ) -> str:
        return await self._send_reference("", card_id, message_id, operation_id)

    async def send_card_id_to_chat(
        self,
        chat_id: str,
        card_id: str,
        *,
        reply_to_message_id: str | None = None,
        operation_id: str | None = None,
    ) -> str:
        if reply_to_message_id:
            if operation_id is None:
                return await self.reply_card_by_id(reply_to_message_id, card_id)
            return await self.reply_card_by_id(reply_to_message_id, card_id, operation_id=operation_id)
        return await self._send_reference(chat_id, card_id, None, operation_id)

    async def _send_reference(
        self,
        chat_id: str,
        card_id: str,
        anchor: str | None,
        operation_id: str | None,
    ) -> str:
        doc = self.mutations.documents.get(card_id)
        token = operation_id or (doc.delivery_ids[0] if doc else uuid.uuid4().hex)
        content = self._dumps({"type": "card", "data": {"card_id": card_id}})
        if doc is not None:
            doc.chat_id, doc.anchor_id = chat_id or doc.chat_id, anchor
        if anchor:
            request = (
                ReplyMessageRequest.builder()
                .message_id(anchor)
                .request_body(
                    ReplyMessageRequestBody.builder().msg_type("interactive").content(content).uuid(token).build()
                )
                .build()
            )
            sdk_call = self._client.im.v1.message.areply
        else:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(content)
                    .uuid(token)
                    .build()
                )
                .build()
            )
            sdk_call = self._client.im.v1.message.acreate
        try:
            resp = await self._message_call("send_card_reference", lambda: sdk_call(request))
            if not resp.data or not resp.data.message_id:
                raise DeliveryUncertain("Card reference response missing message_id")
        except DeliveryUncertain:
            if doc is None:
                raise
            doc.unconfirmed = True
            _logger.warning("Card reference acknowledgement pending: card=%s", card_id[:12])
            # This is an internal edit handle, never a claim of confirmed IM delivery.
            return "hls-pending:" + token
        ident = str(resp.data.message_id)
        if doc is not None:
            doc.messages[0], doc.unconfirmed = ident, False
            if needs_partition(doc.card) and not doc.partitioned:
                await publish_document(self, doc, doc.card)
        return ident

    async def _confirm_reference(self, doc: CardDocument) -> None:
        if doc.unconfirmed:
            await self._send_reference(doc.chat_id, doc.parts[0], doc.anchor_id, doc.delivery_ids[0])

    @property
    def mutations(self) -> CardMutations:
        # SDK-boundary test doubles and older callers can construct via __new__.
        if "_mutations" not in self.__dict__:
            self._mutations = CardMutations()
        return self._mutations

    async def cardkit_create(self, card: dict[str, Any]) -> str:
        if self.mutations.stopping:
            raise RuntimeError("Card creation is disabled during shutdown")
        # Normal initial cards are small. Oversized static initial cards use the
        # same projection, and publish the remainder once their route is known.
        initial = partition_card(card)[0]
        ident = await self._raw_create(initial)
        self.mutations.documents[ident] = CardDocument(card=deepcopy(card), parts=[ident])
        return ident

    async def _raw_create(self, card: dict[str, Any]) -> str:
        """Create one logical card; ambiguous creation is never blindly retried."""
        request = (
            CreateCardRequest.builder()
            .request_body(CreateCardRequestBody.builder().type("card_json").data(self._dumps(card)).build())
            .build()
        )
        resp = await self._checked_call("cardkit_create", lambda: self._client.cardkit.v1.card.acreate(request))
        if resp.data and resp.data.card_id:
            ident = str(resp.data.card_id)
            self.mutations.state(ident).streaming = bool(card.get("config", {}).get("streaming_mode"))
            return ident
        raise FeishuAPIError("cardkit_create: response missing card_id")

    async def _card_settings(self, card_id: str, enabled: bool, sequence: int, operation_id: str) -> None:
        body = (
            SettingsCardRequestBody.builder()
            .settings(self._dumps({"config": {"streaming_mode": enabled}}))
            .sequence(sequence)
            .uuid(operation_id)
            .build()
        )
        request = SettingsCardRequest.builder().card_id(card_id).request_body(body).build()
        await self._checked_call("cardkit_settings", lambda: self._client.cardkit.v1.card.asettings(request))

    async def _mutate(
        self,
        card_id: str,
        call: Any,
        *,
        sequence: int,
        operation_id: str | None,
        stream: bool = False,
        closing: bool = False,
        shutdown: bool = False,
    ) -> None:
        await self.mutations.run(
            card_id,
            sequence=sequence,
            operation_id=operation_id,
            call=call,
            reopen=lambda seq, uid: self._card_settings(card_id, True, seq, uid),
            stream=stream,
            closing=closing,
            shutdown=shutdown,
        )

    async def _raw_stream_element(
        self,
        card_id: str,
        element_id: str,
        content: str,
        *,
        sequence: int = 0,
        operation_id: str | None = None,
    ) -> None:
        """Send a cumulative snapshot, renewing only a live stream."""

        async def call(seq: int, uid: str) -> None:
            body = ContentCardElementRequestBody.builder().content(content).sequence(seq).uuid(uid).build()
            request = (
                ContentCardElementRequest.builder().card_id(card_id).element_id(element_id).request_body(body).build()
            )
            await self._checked_call(
                "cardkit_stream_element",
                lambda: asyncio.to_thread(self._client.cardkit.v1.card_element.content, request),
            )

        # Adopt cards created outside this client only for the legacy direct-call API.
        if card_id not in self.mutations.states:
            self.mutations.state(card_id).streaming = True
        await self._mutate(card_id, call, sequence=sequence, operation_id=operation_id, stream=True)

    async def _raw_update(
        self,
        card_id: str,
        card: dict[str, Any],
        sequence: int = 0,
        *,
        operation_id: str | None = None,
        shutdown: bool = False,
    ) -> None:
        async def call(seq: int, uid: str) -> None:
            body = (
                UpdateCardRequestBody.builder()
                .card(Card.builder().type("card_json").data(self._dumps(card)).build())
                .sequence(seq)
                .uuid(uid)
                .build()
            )
            request = UpdateCardRequest.builder().card_id(card_id).request_body(body).build()
            await self._checked_call("cardkit_update", lambda: self._client.cardkit.v1.card.aupdate(request))

        await self._mutate(
            card_id,
            call,
            sequence=sequence,
            operation_id=operation_id,
            shutdown=shutdown,
            closing=not bool(card.get("config", {}).get("streaming_mode")),
        )

    async def _raw_batch_update(
        self,
        card_id: str,
        actions: list[dict[str, Any]],
        *,
        sequence: int = 0,
        operation_id: str | None = None,
    ) -> None:
        async def call(seq: int, uid: str) -> None:
            body = BatchUpdateCardRequestBody.builder().sequence(seq).actions(self._dumps(actions)).uuid(uid).build()
            request = BatchUpdateCardRequest.builder().card_id(card_id).request_body(body).build()
            await self._checked_call(
                "cardkit_batch_update", lambda: self._client.cardkit.v1.card.abatch_update(request)
            )

        await self._mutate(card_id, call, sequence=sequence, operation_id=operation_id)

    async def _raw_close(
        self,
        card_id: str,
        sequence: int = 0,
        *,
        operation_id: str | None = None,
        shutdown: bool = False,
    ) -> None:
        await self._mutate(
            card_id,
            lambda seq, uid: self._card_settings(card_id, False, seq, uid),
            sequence=sequence,
            operation_id=operation_id,
            closing=True,
            shutdown=shutdown,
        )

    async def cardkit_stream_element(
        self,
        card_id: str,
        element_id: str,
        content: str,
        *,
        sequence: int = 0,
        operation_id: str | None = None,
    ) -> None:
        doc = self.mutations.documents.get(card_id)
        if doc is None:
            await self._raw_stream_element(card_id, element_id, content, sequence=sequence, operation_id=operation_id)
            return
        async with doc.lock:
            if doc.closed:
                raise RuntimeError("Card streaming is already closed")
            candidate = replace_content(doc.card, element_id, content)
            if doc.partitioned or needs_partition(candidate):
                root = self.mutations.state(card_id)
                if root.owner is not None and not root.owner(True):
                    raise RuntimeError("Card stream owner changed")
                await publish_document(self, doc, candidate)
            else:
                await self._raw_stream_element(
                    card_id, element_id, content, sequence=sequence, operation_id=operation_id
                )
                doc.card = candidate
            await self._confirm_reference(doc)

    async def cardkit_update(
        self,
        card_id: str,
        card: dict[str, Any],
        sequence: int = 0,
        *,
        operation_id: str | None = None,
        shutdown: bool = False,
    ) -> None:
        doc = self.mutations.documents.get(card_id)
        if doc is None:
            await self._raw_update(card_id, card, sequence, operation_id=operation_id, shutdown=shutdown)
            return
        async with doc.lock:
            try:
                if doc.partitioned or needs_partition(card):
                    await publish_document(self, doc, card, shutdown=shutdown)
                else:
                    await self._raw_update(card_id, card, sequence, operation_id=operation_id, shutdown=shutdown)
                    doc.card = deepcopy(card)
                await self._confirm_reference(doc)
                doc.write_uncertain = False
            except DeliveryUncertain:
                doc.write_uncertain = True
                raise

    async def cardkit_batch_update(
        self,
        card_id: str,
        actions: list[dict[str, Any]],
        *,
        sequence: int = 0,
        operation_id: str | None = None,
    ) -> None:
        doc = self.mutations.documents.get(card_id)
        if doc is None:
            await self._raw_batch_update(card_id, actions, sequence=sequence, operation_id=operation_id)
            return
        async with doc.lock:
            candidate = apply_actions(doc.card, actions)
            fingerprint = self._dumps(actions)
            pending = doc.pending is not None and doc.pending[0] == fingerprint
            token = doc.pending[1] if pending and doc.pending is not None else operation_id or uuid.uuid4().hex
            if doc.partitioned or needs_partition(candidate):
                await publish_document(self, doc, candidate)
            else:
                try:
                    await self._raw_batch_update(card_id, actions, sequence=sequence, operation_id=token)
                except NETWORK_ERRORS:
                    doc.pending = (fingerprint, token)
                    raise
                except FeishuAPIError as error:
                    if error.code != 300301 or not pending or not any(a["action"] == "add_elements" for a in actions):
                        raise
                    # Only an identical insertion whose earlier result was
                    # unknown may recover via authoritative full replacement.
                    await self._raw_update(card_id, candidate)
                doc.card = candidate
            doc.pending = None
            await self._confirm_reference(doc)

    async def cardkit_close_streaming(
        self,
        card_id: str,
        sequence: int = 0,
        *,
        operation_id: str | None = None,
        shutdown: bool = False,
    ) -> None:
        doc = self.mutations.documents.get(card_id)
        if doc is None:
            await self._raw_close(card_id, sequence, operation_id=operation_id, shutdown=shutdown)
            return
        async with doc.lock:
            doc.closed = True
            for ident in doc.parts:
                await self._raw_close(ident, sequence, operation_id=operation_id, shutdown=shutdown)
            doc.closed = True
            doc.card.setdefault("config", {})["streaming_mode"] = False
            doc.views.clear()

    async def _delete_owned_message(self, message_id: str | None) -> None:
        if message_id is None:
            return
        request = DeleteMessageRequest.builder().message_id(message_id).build()
        response = await self._client.im.v1.message.adelete(request)
        if not response.success() and response.code != MSG_NOT_FOUND:
            self._check(response, "delete rollover message")

    async def upload_image(self, image_url: str) -> str | None:
        """下载远程图片并上传到飞书，返回 img_key."""
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None,
                self._download_image,
                image_url,
            )
        except Exception:
            _logger.debug("image upload failed for %s", image_url, exc_info=True)
            return None

        if data is None:
            return None

        file = io.BytesIO(data)
        request = (
            CreateImageRequest.builder()
            .request_body(CreateImageRequestBody.builder().image_type("message").image(file).build())
            .build()
        )
        resp = await self._client.im.v1.image.acreate(request)
        if resp.success() and resp.data and resp.data.image_key:
            return str(resp.data.image_key)
        return None

    @staticmethod
    def _download_image(url: str, timeout: int = 15) -> bytes | None:
        """同步下载图片（在线程池中运行）."""
        try:
            req = Request(url, headers={"User-Agent": "hermes-lark-streaming/1.0"})
            with urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                return bytes(resp.read())
        except (URLError, OSError):
            _logger.debug("image download failed: %s", url)
            return None
