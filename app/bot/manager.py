"""Telegram bot lifecycle.

The bot reads its token + admin list from the DB ``settings`` table (which the
web panel writes), so admin can be (re)started from the browser without
touching env vars or server files. Fallbacks to ``INIT_BOT_TOKEN`` /
``INIT_BOT_ADMINS`` only on very first boot.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .. import services

logger = logging.getLogger("NexTunnel.bot")

API = "https://api.telegram.org"
_TIMEOUT = httpx.Timeout(55.0, connect=15.0)


class BotManager:
    def __init__(self):
        self.token: str = ""
        self.admins: list[int] = []
        self.running: bool = False
        self._task: asyncio.Task | None = None
        self._offset: int | None = None
        self._last_error: str = ""

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> bool:
        cfg = await services.get_bot_config()
        self.token = (cfg.get("token") or "").strip()
        self.admins = [int(a) for a in cfg.get("admins", [])]
        if not self.token:
            self.running = False
            return False
        self.running = True
        self._task = asyncio.get_event_loop().create_task(self._poll())
        logger.info("Telegram bot started")
        return True

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("Telegram bot stopped")

    async def restart(self) -> bool:
        await self.stop()
        return await self.start()

    async def _post(self, method: str, **data) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(f"{API}/bot{self.token}/{method}", json=data)
                if r.status_code == 409:  # conflict with another poller
                    logger.warning("Telegram poll conflict (another instance?)")
                    self._last_error = "poll conflict"
                    self.running = False
                    return None
                if r.status_code != 200:
                    self._last_error = f"tg {r.status_code}"
                    logger.error("Telegram %s failed: %s %s", method, r.status_code, r.text[:200])
                    return None
                return r.json()
        except httpx.HTTPError as exc:
            self._last_error = str(exc)
            logger.error("Telegram request error: %s", exc)
            return None

    # ── polling loop ───────────────────────────────────────────────────────
    async def _poll(self):
        while self.running:
            res = await self._post(
                "getUpdates",
                offset=self._offset,
                timeout=50,  # long-poll seconds (server-side)
                allowed_updates=["message"],
            )
            if res is not None and res.get("ok"):
                for upd in res.get("result", []):
                    self._offset = upd["update_id"] + 1
                    await self._handle(upd.get("message") or {})
            else:
                await asyncio.sleep(3)

    async def _handle(self, message: dict):
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not text or chat_id is None:
            return
        if not text.startswith("/"):
            return
        if chat_id not in self.admins:
            await self._send(chat_id, "⛔️ شما مجاز نیستید.")
            return
        reply = await services_handle_command(text)
        await self._send(chat_id, reply or "دستور نامفهوم است. /help")

    async def _send(self, chat_id: int, text: str):
        if len(text) > 4000:
            text = text[:3990] + "…"
        await self._post("sendMessage", chat_id=chat_id, text=text)


manager = BotManager()


from .handlers import services_handle_command