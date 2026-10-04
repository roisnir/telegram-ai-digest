"""Operator alerts: the one place that knows what an alert looks like.

Every alert is built and sent from here, so they all share the same window
line, length limit and plain-text rule. Adding an alert kind means adding one
method to ``Alerts``. Sending is best-effort: nothing here raises, so an alert
that can't be delivered never breaks a run or hides the error it reports.
"""
import logging
from datetime import datetime
from typing import Any

from pytz import timezone
from telethon import TelegramClient
from telethon.sessions import StringSession

LOCAL_TZ = timezone('Asia/Jerusalem')

_ALERT_ERROR_MAXLEN = 400
_UNKNOWN_WINDOW = "unknown window"


def _coerce_chat_id(raw: str) -> int | str:
    """Coerce a configured chat id to what Telethon expects.

    Same convention as TARGET_CHANNEL: a bare (optionally negative) number is
    passed as an int; anything else — '@username' or the literal 'me' — is
    passed through as a string for Telethon to resolve.
    """
    raw = (raw or "").strip()
    return int(raw) if raw.lstrip('-').isdigit() else raw


def format_window(start_date: datetime | None, end_date: datetime | None) -> str:
    """Human-readable local-time description of the window being processed."""
    if end_date is None:
        return _UNKNOWN_WINDOW
    end_str = end_date.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')
    if start_date is None:
        return f"up to {end_str} Israel"
    start_str = start_date.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')
    return f"{start_str} -> {end_str} Israel"


def format_failure_alert(error: BaseException | str, window: str, stage: str = "") -> str:
    """Short, phone-skimmable alert for a run that produced no digest."""
    if isinstance(error, BaseException):
        detail = f"{type(error).__name__}: {error}"
    else:
        detail = str(error)
    if len(detail) > _ALERT_ERROR_MAXLEN:
        detail = detail[:_ALERT_ERROR_MAXLEN] + "…"
    lines = ["🚨 Digest run FAILED — nothing was published.", f"Window: {window}"]
    if stage:
        lines.append(f"Stage: {stage}")
    lines.append(f"Error: {detail}")
    return "\n".join(lines)


def format_health_alert(
    issues: list[str],
    coverage: dict[str, Any],
    window: str,
    page_url: str | None = None,
) -> str:
    """Short, phone-skimmable alert for a digest that published but looks wrong."""
    total = coverage.get("total", 0)
    covered = coverage.get("covered", 0)
    pct = (100.0 * covered / total) if total else 0.0
    lines = [
        "⚠️ Digest published but looks wrong.",
        f"Window: {window}",
        f"Coverage: {covered}/{total} ({pct:.0f}%)",
    ]
    lines += [f"• {issue}" for issue in issues]
    if page_url:
        lines.append(f"Page: {page_url}")
    return "\n".join(lines)


class Alerts:
    """Operator alerts for one run. Off entirely when ``chat_id`` is falsy.

    Set ``window`` as soon as the run's window is known, and ``client`` once the
    user session is connected. Until then alerts say "unknown window" and, if
    there is no connected client, start their own user session.

    ``sender`` replaces the Telegram delivery (tests pass a fake). Each public
    method returns True if the alert was sent.
    """

    def __init__(
        self,
        chat_id: str | None,
        *,
        bot_token: str | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
        phone: str | None = None,
        sender=None,
    ):
        self.chat_id = chat_id
        self.bot_token = bot_token
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.window = _UNKNOWN_WINDOW
        self.client = None
        self._sender = sender or self._send_telethon

    async def failed(self, error: BaseException | str, stage: str = "") -> bool:
        return await self._send(format_failure_alert(error, self.window, stage))

    async def unhealthy(self, issues: list[str], coverage: dict[str, Any], page_url: str | None = None) -> bool:
        return await self._send(format_health_alert(issues, coverage, self.window, page_url))

    async def no_messages(self) -> bool:
        return await self._send(
            f"⚠️ No digest published.\nWindow: {self.window}\n"
            "No messages were fetched from any channel — quiet window, or a broken fetch."
        )

    async def whatsapp_failed(self, wa_error: str) -> bool:
        return await self._send(f"⚠️ {wa_error}\nTelegram delivery was fine.")

    async def _send(self, text: str) -> bool:
        """Best-effort send of ``text``. Never raises."""
        if not self.chat_id:
            return False
        try:
            await self._sender(text)
            return True
        except Exception as e:
            logging.error(
                f"Failed to send operator alert ({type(e).__name__}: {e}). "
                f"Alert text was: {text}"
            )
            return False

    async def _send_telethon(self, text: str) -> None:
        """Send via the bot if BOT_TOKEN is set, else the connected user session, else a fresh one.

        Sent as plain text on purpose: the digest message uses parse_mode='html',
        but alert bodies embed arbitrary exception text that would break HTML
        parsing and silently drop the alert.
        """
        bot = None
        own_client = None
        try:
            target = _coerce_chat_id(self.chat_id)
            if self.bot_token:
                bot = await TelegramClient(StringSession(), self.api_id, self.api_hash).start(bot_token=self.bot_token)
                sender = bot
            elif self.client is not None:
                sender = self.client
            else:
                own_client = TelegramClient('session', self.api_id, self.api_hash)
                await own_client.start(phone=self.phone)
                sender = own_client
            await sender.send_message(target, text)
            logging.info(f"Operator alert sent to {target}")
        finally:
            for c in (bot, own_client):
                if c is None:
                    continue
                try:
                    await c.disconnect()
                except Exception as e:
                    logging.warning(f"Failed to disconnect alert client: {e}")
