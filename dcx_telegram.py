"""
CoinDCX Bot — Telegram Interface
==================================
Commands:
  /start    — greeting + help
  /status   — capital, P&L, open count, mode
  /positions — list open positions
  /pause    — stop opening new trades
  /resume   — resume trading
  /stop     — alias for /pause
  /chatid   — print chat + user IDs (no auth gate)
  /help     — command list

Only messages from the configured TELEGRAM_CHAT_ID are accepted.
"""

import asyncio
import logging
from typing import Callable, Optional

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)


class DCXTelegramBot:
    def __init__(self, token: str, chat_id: str):
        self._enabled   = bool(token and chat_id)
        self._chat_id   = str(chat_id)
        self.paused     = False

        self._status_provider: Optional[Callable[[], str]]    = None
        self._positions_provider: Optional[Callable[[], str]] = None

        self._risk_setter: Optional[Callable]     = None
        self._daily_provider: Optional[Callable]  = None
        self._close_fn: Optional[Callable]        = None
        self._symbols_provider: Optional[Callable]= None

        if self._enabled:
            self._app = Application.builder().token(token).build()
            self._register_handlers()
        else:
            self._app = None
            logger.warning(
                "Telegram disabled — set TELEGRAM_TOKEN + TELEGRAM_CHAT_ID in .env"
            )

    # ── Provider injection ─────────────────────────────────────────────────────

    def set_status_provider(self, fn: Callable[[], str]):
        self._status_provider = fn

    def set_positions_provider(self, fn: Callable[[], str]):
        self._positions_provider = fn

    def set_risk_setter(self, fn: Callable[[float], None]):
        self._risk_setter = fn

    def set_daily_provider(self, fn: Callable[[], str]):
        self._daily_provider = fn

    def set_close_fn(self, fn: Callable[[str], str]):
        self._close_fn = fn

    def set_symbols_provider(self, fn: Callable[[], str]):
        self._symbols_provider = fn

    # ── Auth guard ─────────────────────────────────────────────────────────────

    def _authorized(self, update: Update) -> bool:
        return str(update.effective_user.id) == self._chat_id

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _reply(self, text: str):
        await self._app.bot.send_message(
            chat_id    = self._chat_id,
            text       = text,
            parse_mode = ParseMode.MARKDOWN,
        )

    # ── Handlers ───────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        await self._reply(
            "👋 *CoinDCX Futures Bot*\n"
            "─────────────────────────────\n"
            "/status    — capital & P&L\n"
            "/positions — open positions\n"
            "/daily     — today's summary\n"
            "/symbols   — watched symbols\n"
            "/risk 2    — set risk %\n"
            "/close ETH — close a position\n"
            "/pause     — stop new trades\n"
            "/resume    — resume trading\n"
            "/help      — this message",
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        await self._reply(
            "🤖 *CoinDCX Futures Bot Commands*\n"
            "─────────────────────────────\n"
            "/status    — capital, P&L, win rate\n"
            "/positions — list open positions\n"
            "/daily     — today's P&L summary\n"
            "/symbols   — watched symbols & open status\n"
            "/risk 2    — set risk per trade to 2%\n"
            "/close ETH — force-close ETH position\n"
            "/pause     — halt new trade entries\n"
            "/resume    — re-enable entries\n"
            "/stop      — alias for /pause\n"
            "/help      — this message",
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        text = self._status_provider() if self._status_provider else "Status unavailable."
        await self._reply(text)

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        text = self._positions_provider() if self._positions_provider else "Positions unavailable."
        await self._reply(text)

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        self.paused = True
        await self._reply(
            "⏸ Trading *paused* — existing positions still tracked.\n"
            "Send /resume to restart entries.",
        )

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        self.paused = False
        await self._reply("▶️ Trading *resumed*.")

    async def _cmd_risk(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        if not ctx.args:
            await self._reply("Usage: /risk <percent>  e.g. /risk 2")
            return
        try:
            pct = float(ctx.args[0])
        except ValueError:
            await self._reply("❌ Invalid value. Example: /risk 2")
            return
        if not (0.5 <= pct <= 20):
            await self._reply("❌ Risk must be between 0.5 and 20.")
            return
        if self._risk_setter:
            self._risk_setter(pct)
            await self._reply(f"✅ Risk per trade set to *{pct}%*.")
        else:
            await self._reply("Risk setter not available.")

    async def _cmd_daily(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        text = self._daily_provider() if self._daily_provider else "Daily summary unavailable."
        await self._reply(text)

    async def _cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        if not ctx.args:
            await self._reply("Usage: /close <symbol>  e.g. /close ETH or /close B-ETH_USDT")
            return
        sym = ctx.args[0].upper()
        if not sym.startswith("B-"):
            sym = f"B-{sym}_USDT"
        if self._close_fn:
            result = self._close_fn(sym)
            await self._reply(result)
        else:
            await self._reply("Close function not available.")

    async def _cmd_symbols(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorized(update):
            return
        text = self._symbols_provider() if self._symbols_provider else "Symbols unavailable."
        await self._reply(text)

    async def _cmd_chatid(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        user = update.effective_user
        await update.message.reply_text(
            f"🆔 *Chat ID:* `{chat.id}`\n"
            f"👤 *User ID:* `{user.id}`\n\n"
            f"Set `TELEGRAM_CHAT_ID={user.id}` in .env",
            parse_mode=ParseMode.MARKDOWN,
        )

    def _register_handlers(self):
        self._app.add_handler(CommandHandler("start",     self._cmd_start))
        self._app.add_handler(CommandHandler("help",      self._cmd_help))
        self._app.add_handler(CommandHandler("status",    self._cmd_status))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("pause",     self._cmd_pause))
        self._app.add_handler(CommandHandler("stop",      self._cmd_pause))
        self._app.add_handler(CommandHandler("resume",    self._cmd_resume))
        self._app.add_handler(CommandHandler("risk",      self._cmd_risk))
        self._app.add_handler(CommandHandler("daily",     self._cmd_daily))
        self._app.add_handler(CommandHandler("close",     self._cmd_close))
        self._app.add_handler(CommandHandler("symbols",   self._cmd_symbols))
        self._app.add_handler(CommandHandler("chatid",    self._cmd_chatid))

    # ── Outbound ───────────────────────────────────────────────────────────────

    async def send(self, text: str):
        if not self._enabled:
            logger.info(f"[TELEGRAM]\n{text}\n")
            return
        try:
            await self._app.bot.send_message(
                chat_id    = self._chat_id,
                text       = text,
                parse_mode = ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start_polling(self):
        if not self._enabled:
            return
        await self._app.initialize()
        await self._app.start()
        await self._app.bot.set_my_commands([
            BotCommand("status",    "Capital & P&L"),
            BotCommand("positions", "Open positions"),
            BotCommand("daily",     "Today's P&L summary"),
            BotCommand("symbols",   "Watched symbols & open status"),
            BotCommand("risk",      "Set risk % (e.g. /risk 2)"),
            BotCommand("close",     "Force-close position (e.g. /close ETH)"),
            BotCommand("pause",     "Pause new trades"),
            BotCommand("resume",    "Resume trading"),
            BotCommand("help",      "Command list"),
        ])
        await self._app.updater.start_polling(
            allowed_updates    = ["message"],
            drop_pending_updates = True,
        )
        logger.info("Telegram polling started")
        while self._app.updater.running:
            await asyncio.sleep(1)

    async def stop(self):
        if self._app and self._app.updater.running:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
