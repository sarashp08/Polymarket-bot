"""
Telegram bot — outbound notifications + inbound phone commands.

Sending:
  bot.send(text)  — push any message to your chat

Commands (send from your phone):
  /status    — bankroll, session P&L, open count, paused state
  /positions — list every open paper position with time remaining
  /pause     — pause opening new trades (existing ones still resolve)
  /resume    — resume trading
  /help      — show available commands

Only messages from the configured TELEGRAM_CHAT_ID are accepted.
When credentials are absent, all messages fall back to console logging.
"""

import asyncio
import logging
from typing import Callable, Optional

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self._enabled = bool(token and chat_id)
        self._chat_id = str(chat_id)
        self.paused = False

        # Callables injected by main.py after all components are wired
        self._status_provider: Optional[Callable[[], str]] = None
        self._positions_provider: Optional[Callable[[], str]] = None

        if self._enabled:
            self._app = Application.builder().token(token).build()
            self._register_handlers()
        else:
            self._app = None
            logger.warning(
                "Telegram not configured — set TELEGRAM_TOKEN + TELEGRAM_CHAT_ID "
                "in .env to enable notifications"
            )

    # ── Provider injection ─────────────────────────────────────────────────────

    def set_status_provider(self, fn: Callable[[], str]):
        self._status_provider = fn

    def set_positions_provider(self, fn: Callable[[], str]):
        self._positions_provider = fn

    # ── Auth guard ────────────────────────────────────────────────────────────

    def _authorized(self, update: Update) -> bool:
        return str(update.effective_chat.id) == self._chat_id

    # ── Command handlers ──────────────────────────────────────────────────────

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        await update.message.reply_text(
            "🤖 *Polymarket Bot Commands*\n"
            "─────────────────────────────\n"
            "/status — bankroll & session stats\n"
            "/positions — list open positions\n"
            "/pause — stop opening new trades\n"
            "/resume — resume trading\n"
            "/help — this message",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        text = self._status_provider() if self._status_provider else "Status unavailable."
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        text = self._positions_provider() if self._positions_provider else "Positions unavailable."
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        self.paused = True
        await update.message.reply_text(
            "⏸ Trading *paused* — existing positions still resolve normally.\n"
            "Send /resume to restart.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        self.paused = False
        await update.message.reply_text(
            "▶️ Trading *resumed*.",
            parse_mode=ParseMode.MARKDOWN,
        )

    def _register_handlers(self):
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))

    # ── Outbound ──────────────────────────────────────────────────────────────

    async def send(self, text: str):
        if not self._enabled:
            logger.info(f"[TELEGRAM]\n{text}\n")
            return
        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.error(f"Telegram send error: {exc}")

    # ── Polling loop ──────────────────────────────────────────────────────────

    async def start_polling(self):
        """Start receiving commands. Runs concurrently via asyncio.gather()."""
        if not self._enabled:
            return
        await self._app.initialize()
        await self._app.start()
        # Register command menu shown in the Telegram compose bar
        await self._app.bot.set_my_commands([
            BotCommand("status",    "Bankroll & session stats"),
            BotCommand("positions", "List open positions"),
            BotCommand("pause",     "Pause opening new trades"),
            BotCommand("resume",    "Resume trading"),
            BotCommand("help",      "Show available commands"),
        ])
        await self._app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True,
        )
        logger.info("Telegram command polling started — send /help to your bot")
        # Hold coroutine open until stop() is called externally
        while self._app.updater.running:
            await asyncio.sleep(1)

    async def stop(self):
        if self._app and self._app.updater.running:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
