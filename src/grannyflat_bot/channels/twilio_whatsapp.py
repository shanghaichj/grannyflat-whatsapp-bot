from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from base64 import b64encode
from typing import Any
from xml.sax.saxutils import escape

import httpx

from grannyflat_bot.catalog.packs import CatalogPack, pack_to_twilio_products
from grannyflat_bot.channels.base import Channel, InboundMessage, OutboundMessage
from grannyflat_bot.config import DATA_DIR, Settings, get_settings

TWILIO_API = "https://api.twilio.com/2010-04-01"
TWILIO_CONTENT_API = "https://content.twilio.com/v1"
TWILIO_MESSAGING_API = "https://messaging.twilio.com/v1"
MAX_WHATSAPP_CHARS = 1500
_CONTENT_STATIC_CACHE = DATA_DIR / "catalog" / "twilio_content_sid_static_v3.txt"
_MSID_CACHE = DATA_DIR / "catalog" / "twilio_messaging_service_sid.txt"


class TwilioWhatsAppChannel(Channel):
    """Twilio WhatsApp: parse inbound webhooks and send replies."""

    name = "twilio_whatsapp"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def parse_twilio_form(self, form: dict[str, str]) -> InboundMessage:
        from_number = form.get("From", "").removeprefix("whatsapp:")
        return InboundMessage(
            session_id=f"wa-{from_number}",
            text=form.get("Body", "").strip(),
            sender_id=from_number,
            channel=self.name,
            raw=dict(form),
        )

    def verify_signature(
        self,
        url: str,
        params: dict[str, str],
        signature: str,
    ) -> bool:
        token = self.settings.twilio_auth_token
        if not token or not signature:
            return False
        pieces = url
        for key in sorted(params):
            pieces += key + params[key]
        digest = hmac.new(
            token.encode("utf-8"),
            pieces.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        expected = b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, signature)

    def build_twiml(self, text: str, status_callback: str | None = None) -> str:
        body = text.strip()
        if len(body) > MAX_WHATSAPP_CHARS:
            body = body[: MAX_WHATSAPP_CHARS - 3] + "..."
        callback_attr = ""
        if status_callback:
            escaped_cb = escape(status_callback, {'"': "&quot;"})
            callback_attr = f' statusCallback="{escaped_cb}"'
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Message{callback_attr}>{escape(body)}</Message></Response>"
        )

    async def send_content_template(
        self,
        *,
        to: str,
        content_sid: str,
        content_variables: dict[str, Any] | None = None,
        status_callback: str | None = None,
    ) -> dict[str, Any]:
        """发送官方 Twilio Content 模板（如 60/70/80㎡ 报价模板）。"""
        return await self._post_content_message(
            to=to,
            content_sid=content_sid,
            content_variables=content_variables,
            status_callback=status_callback,
        )

    async def send_text(self, message: OutboundMessage) -> dict[str, Any]:
        if not self.settings.twilio_whatsapp_enabled:
            raise RuntimeError("Twilio WhatsApp is paused (TWILIO_WHATSAPP_ENABLED=0)")
        to = self._normalize_to(message.metadata.get("to"))
        account_sid = self.settings.twilio_account_sid
        if not account_sid:
            raise ValueError("TWILIO_ACCOUNT_SID is not set")

        body = message.text.strip()
        if len(body) > MAX_WHATSAPP_CHARS:
            body = body[: MAX_WHATSAPP_CHARS - 3] + "..."

        data: dict[str, str] = {"To": to, "Body": body}
        if message.metadata.get("status_callback"):
            data["StatusCallback"] = str(message.metadata["status_callback"])
        data.update(self._text_from_fields())

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{TWILIO_API}/Accounts/{account_sid}/Messages.json",
                auth=self._rest_auth(),
                data=data,
            )
            resp.raise_for_status()
            return resp.json()

    async def send_catalog(self, *, to: str, pack: CatalogPack) -> dict[str, Any]:
        """发送 WhatsApp 多商品目录。

        1) 固定多商品 Content
        2) 失败则逐个单品
        3) 仍 63013 则发目录链接文本（客户可点开浏览全部商品）
        """
        if not self.settings.twilio_whatsapp_enabled:
            raise RuntimeError("Twilio WhatsApp is paused (TWILIO_WHATSAPP_ENABLED=0)")
        mpm_err: Exception | None = None
        try:
            return await self._send_catalog_static_mpm(to=to, pack=pack)
        except Exception as exc:  # noqa: BLE001
            mpm_err = exc

        results: list[dict[str, Any]] = []
        last_err: Exception | None = None
        for item in pack.items:
            try:
                one = await self._send_catalog_single(
                    to=to, pack=pack, item_id=item.content_id
                )
                results.append({"name": item.name, "sid": one.get("sid"), "ok": True})
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                results.append({"name": item.name, "ok": False, "error": str(exc)[:200]})
        ok_count = sum(1 for r in results if r.get("ok"))
        if ok_count > 0:
            return {
                "sid": results[0].get("sid"),
                "mode": "single_product_fallback",
                "items_sent": ok_count,
                "details": results,
                "mpm_error": str(mpm_err)[:300] if mpm_err else None,
            }

        # Twilio Content 商品消息被拒（常见 63013）→ 发目录链接，保证客户能打开
        link_result = await self._send_catalog_link_fallback(to=to, pack=pack)
        link_result["mpm_error"] = str(mpm_err)[:300] if mpm_err else None
        link_result["spm_error"] = str(last_err)[:300] if last_err else None
        return link_result

    async def _send_catalog_link_fallback(
        self, *, to: str, pack: CatalogPack
    ) -> dict[str, Any]:
        """用普通文本 + wa.me 目录链接作为兜底（不依赖 Product Message）。"""
        from_num = (self.settings.twilio_whatsapp_from or "").removeprefix("whatsapp:")
        digits = "".join(ch for ch in from_num if ch.isdigit())
        catalog_url = f"https://wa.me/c/{digits}" if digits else "https://wa.me/"
        mobile_url = "https://www.ausgrannyflat.com/m/60"
        names = ", ".join(item.name for item in pack.items)
        body = (
            f"Here are our {pack.title} floor plans: {names}.\n\n"
            f"Browse on your phone:\n{mobile_url}\n\n"
            f"Or open the WhatsApp catalog:\n{catalog_url}\n\n"
            f"Reply with the model code (e.g. A60) if you would like more details."
        )
        to_addr = self._normalize_to(to)
        account_sid = self.settings.twilio_account_sid
        data: dict[str, str] = {"To": to_addr, "Body": body}
        # 文本兜底：优先用 From 号码直发（与普通客服消息一致）
        try:
            data.update(self._text_from_fields())
        except ValueError:
            data.update(await self._catalog_from_fields())

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{TWILIO_API}/Accounts/{account_sid}/Messages.json",
                auth=self._rest_auth(),
                data=data,
            )
            if resp.status_code >= 400:
                raise ValueError(
                    "商品卡片与目录链接均发送失败。"
                    f"卡片错误见 63013；链接错误：{resp.text[:300]}"
                )
            created = resp.json()
        created = await self._require_delivered(created, mode="catalog_link_fallback")
        created["catalog_url"] = catalog_url
        created["note"] = (
            "Twilio 拒绝商品卡片（63013）。已改为发送目录链接，"
            "客户点击可浏览全部商品。若需原生商品卡片，请向 Twilio Support "
            "提交失败 Message SID 并申请开通/排查 Catalog Product Message。"
        )
        return created

    async def _send_catalog_static_mpm(self, *, to: str, pack: CatalogPack) -> dict[str, Any]:
        content_sid = await self.ensure_static_catalog_content_sid(pack)
        created = await self._post_content_message(
            to=to,
            content_sid=content_sid,
            content_variables=None,
        )
        return await self._require_delivered(created, mode="static_mpm")

    async def _send_catalog_single(
        self, *, to: str, pack: CatalogPack, item_id: str
    ) -> dict[str, Any]:
        content_sid = await self._create_ephemeral_single_product(pack=pack, item_id=item_id)
        created = await self._post_content_message(
            to=to,
            content_sid=content_sid,
            content_variables=None,
        )
        return await self._require_delivered(created, mode="single")

    async def _post_content_message(
        self,
        *,
        to: str,
        content_sid: str,
        content_variables: dict[str, Any] | None,
        status_callback: str | None = None,
    ) -> dict[str, Any]:
        to_addr = self._normalize_to(to)
        account_sid = self.settings.twilio_account_sid
        if not account_sid:
            raise ValueError("TWILIO_ACCOUNT_SID is not set")
        data: dict[str, str] = {
            "To": to_addr,
            "ContentSid": content_sid,
        }
        if content_variables:
            data["ContentVariables"] = json.dumps(content_variables, ensure_ascii=True)
        if status_callback:
            data["StatusCallback"] = status_callback
        data.update(await self._catalog_from_fields())

        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{TWILIO_API}/Accounts/{account_sid}/Messages.json",
                auth=self._rest_auth(),
                data=data,
            )
            if resp.status_code >= 400:
                raise ValueError(
                    f"Twilio catalog send failed ({resp.status_code}): {resp.text[:500]}"
                )
            return resp.json()

    async def _require_delivered(self, created: dict[str, Any], *, mode: str) -> dict[str, Any]:
        msg_sid = str(created.get("sid") or "")
        status = await self._wait_message_status(msg_sid)
        err_code = status.get("error_code")
        err_msg = status.get("error_message") or ""
        final = str(status.get("status") or created.get("status") or "")
        if final in ("failed", "undelivered") or err_code:
            raise ValueError(self._explain_delivery_error(err_code, err_msg, final))
        created["delivery_status"] = final
        created["mode"] = mode
        return created

    async def ensure_static_catalog_content_sid(self, pack: CatalogPack) -> str:
        configured = (self.settings.twilio_catalog_content_sid or "").strip()
        if configured:
            return configured

        if _CONTENT_STATIC_CACHE.exists():
            cached = _CONTENT_STATIC_CACHE.read_text(encoding="utf-8").strip()
            if cached.startswith("HX"):
                return cached

        catalog_id = (self.settings.whatsapp_catalog_id or "").strip()
        if not catalog_id:
            raise ValueError("WHATSAPP_CATALOG_ID 未配置")

        items = pack_to_twilio_products(pack)
        payload = {
            "friendly_name": f"grannyflat-static-{pack.key}-v3",
            "language": "en",
            "types": {
                "twilio/catalog": {
                    "id": catalog_id,
                    "title": pack.title[:60],
                    "body": pack.body[:1024],
                    "subtitle": pack.subtitle[:1024],
                    "thumbnail_item_id": pack.thumbnail_id,
                    "items": items,
                }
            },
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{TWILIO_CONTENT_API}/Content",
                auth=self._rest_auth(),
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code >= 400:
                raise ValueError(
                    f"Create static catalog Content failed ({resp.status_code}): {resp.text[:400]}"
                )
            sid = str(resp.json().get("sid") or "").strip()
        if not sid.startswith("HX"):
            raise ValueError("Twilio Content API did not return Content SID")
        _CONTENT_STATIC_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CONTENT_STATIC_CACHE.write_text(sid + "\n", encoding="utf-8")
        return sid

    async def _create_ephemeral_single_product(
        self, *, pack: CatalogPack, item_id: str
    ) -> str:
        catalog_id = (self.settings.whatsapp_catalog_id or "").strip()
        if not catalog_id:
            raise ValueError("WHATSAPP_CATALOG_ID 未配置")
        payload = {
            "friendly_name": f"grannyflat-spm-{item_id}",
            "language": "en",
            "types": {
                "twilio/catalog": {
                    "id": catalog_id,
                    "body": pack.body[:1024],
                    "items": [{"id": item_id}],
                }
            },
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{TWILIO_CONTENT_API}/Content",
                auth=self._rest_auth(),
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code >= 400:
                raise ValueError(
                    f"Create SPM Content failed ({resp.status_code}): {resp.text[:300]}"
                )
            sid = str(resp.json().get("sid") or "").strip()
        if not sid.startswith("HX"):
            raise ValueError("SPM Content SID missing")
        return sid

    async def _wait_message_status(
        self, message_sid: str, *, attempts: int = 10, delay_s: float = 0.9
    ) -> dict[str, Any]:
        if not message_sid:
            return {}
        account_sid = self.settings.twilio_account_sid
        url = f"{TWILIO_API}/Accounts/{account_sid}/Messages/{message_sid}.json"
        last: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            for _ in range(attempts):
                resp = await client.get(url, auth=self._rest_auth())
                if resp.status_code >= 400:
                    break
                last = resp.json()
                status = str(last.get("status") or "")
                if status in ("delivered", "read", "failed", "undelivered", "canceled"):
                    break
                if last.get("error_code"):
                    break
                await asyncio.sleep(delay_s)
        return last

    def _explain_delivery_error(
        self, err_code: Any, err_msg: str, status: str
    ) -> str:
        code = str(err_code) if err_code is not None else ""
        msid = (self.settings.twilio_messaging_service_sid or "").strip()
        if code == "63013":
            hint = []
            if not msid:
                hint.append("服务器未配置 TWILIO_MESSAGING_SERVICE_SID")
            hint.append(
                "请到 Meta Commerce Manager 打开 A60/B60/C60/D60，确认商品状态为「有效/已发布」，"
                "价格、图片、链接齐全且无违规（有红色感叹号需先申诉）"
            )
            hint.append(
                "也可在 Twilio Console → Monitor → Logs → Messaging 点开失败消息 SID 查看 Meta 原始错误"
            )
            return (
                f"Twilio/WhatsApp 拒绝商品目录（63013），status={status}。 "
                + "；".join(hint)
                + (f"；detail={err_msg}" if err_msg else "")
            )
        return (
            f"WhatsApp 商品目录投递失败：status={status} "
            f"error={code or 'none'} {err_msg}"
        ).strip()

    def _normalize_to(self, to: str | None) -> str:
        if not to:
            raise ValueError("Outbound to phone is required")
        to = to.strip()
        if not to.startswith("whatsapp:"):
            to = f"whatsapp:{to}"
        return to

    def _text_from_fields(self) -> dict[str, str]:
        from_ = (self.settings.twilio_whatsapp_from or "").strip()
        if not from_:
            raise ValueError("TWILIO_WHATSAPP_FROM is not set")
        if not from_.startswith("whatsapp:") and not from_.startswith("MG"):
            from_ = f"whatsapp:{from_}"
        if from_.startswith("MG"):
            return {"MessagingServiceSid": from_}
        return {"From": from_}

    async def _catalog_from_fields(self) -> dict[str, str]:
        """Use the configured sender, just like text replies; avoid stale service pools."""
        return self._text_from_fields()

    async def _resolve_messaging_service_sid(self) -> str:
        if _MSID_CACHE.exists():
            cached = _MSID_CACHE.read_text(encoding="utf-8").strip()
            if cached.startswith("MG"):
                return cached
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{TWILIO_MESSAGING_API}/Services",
                auth=self._rest_auth(),
                params={"PageSize": 20},
            )
            if resp.status_code >= 400:
                return ""
            services = resp.json().get("services") or []
        # 优先「奶奶房客服」
        for svc in services:
            name = str(svc.get("friendly_name") or "")
            sid = str(svc.get("sid") or "")
            if "奶奶" in name and sid.startswith("MG"):
                _MSID_CACHE.parent.mkdir(parents=True, exist_ok=True)
                _MSID_CACHE.write_text(sid + "\n", encoding="utf-8")
                return sid
        for svc in services:
            sid = str(svc.get("sid") or "")
            if sid.startswith("MG"):
                return sid
        return ""

    def _rest_auth(self) -> tuple[str, str]:
        if self.settings.twilio_api_key_sid and self.settings.twilio_api_key_secret:
            return (
                self.settings.twilio_api_key_sid,
                self.settings.twilio_api_key_secret,
            )
        if self.settings.twilio_account_sid and self.settings.twilio_auth_token:
            return (
                self.settings.twilio_account_sid,
                self.settings.twilio_auth_token,
            )
        raise ValueError(
            "Set TWILIO_API_KEY_SID + TWILIO_API_KEY_SECRET "
            "or TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN"
        )

    async def receive(self) -> InboundMessage | None:
        return None
