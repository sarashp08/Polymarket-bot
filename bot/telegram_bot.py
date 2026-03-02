"""
Telegram notifier.

Wraps python-telegram-bot's async Bot.send_message.
When TELEGRAM_TOKEN / TELEGRAM_CHAT_ID are not set, messages are
logged locally instead so the bot still runs without Telegram.
"""

import logging

from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self._enabled = bool(token and chat_id)
        self._chat_id = chat_id
        self._bot = Bot(token=token) if self._enabled else None

        if not self._enabled:
            logger.warning(
                "Telegram not configured — set TELEGRAM_TOKEN + TELEGRAM_CHAT_ID "
                "in .env to enable notifications"
            )

    async def send(self, text: str):
        if not self._enabled:
            # Print to console so the user can still see signals during dev
            logger.info(f"[TELEGRAM]\n{text}\n")
            return
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.error(f"Telegram send error: {exc}")
