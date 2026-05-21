#!/usr/bin/env python3
"""
MÓDULO TELEGRAM BOT — Cerebro Quant v5
Bot interactivo con comandos y chat conversacional vía Groq
"""

import asyncio
import logging
import json
from groq import AsyncGroq
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

logger = logging.getLogger("TelegramBot")


class TelegramBot:
    def __init__(self, config: dict, session, macro, forense, veto):
        self.config = config
        self.session = session
        self.macro = macro
        self.forense = forense
        self.veto = veto

        tg = config.get("telegram", {})
        # Support both "token" and "bot_token" field names
        self.token = tg.get("token", tg.get("bot_token", ""))
        self.chat_id = str(tg.get("chat_id", ""))
        self.groq_key = config.get("groq", {}).get("api_key", "")
        self.groq_model = config.get("groq", {}).get("model", "llama-3.3-70b-versatile")

        if self.token:
            try:
                self.application = Application.builder().token(self.token).build()
                self._register_handlers()
                logger.info("TelegramBot initialized")
            except Exception as exc:
                logger.error("TelegramBot init error: %s — bot disabled", exc)
                self.application = None
        else:
            logger.warning("TelegramBot: no token configured — bot disabled")
            self.application = None

    def _register_handlers(self) -> None:
        if self.application is None:
            return
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("status", self.cmd_status))
        self.application.add_handler(CommandHandler("macro", self.cmd_macro))
        self.application.add_handler(CommandHandler("forense", self.cmd_forense))
        self.application.add_handler(CommandHandler("ayuda", self.cmd_ayuda))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text)
        )

    # ─── COMMANDS ────────────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = (
            "```\n"
            "╔═══════════════════════════════════════╗\n"
            "║   🧠 CEREBRO QUANT v5 — EN LÍNEA      ║\n"
            "╚═══════════════════════════════════════╝\n\n"
            "Comandos disponibles:\n\n"
            "  /status  →  Estado del mercado y sesión NY\n"
            "  /macro   →  Calendario económico del día\n"
            "  /forense →  Análisis de pérdidas históricas\n"
            "  /ayuda   →  Este menú\n\n"
            "O escríbeme directamente para hablar con la IA.\n"
            "```"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        pin = self.session.get_risk_pin()
        bar = self.session.get_progress_bar(width=18)
        countdown = self.session.get_countdown_str()
        ny_time = self.session.get_ny_now().strftime("%I:%M:%S %p")
        open_str = "✅ ABIERTO" if self.session.is_market_open() else "🔴 CERRADO"
        macro_auth, macro_reason = self.macro.authorize_trade()
        next_ev = self.macro.get_next_event()

        pin_emoji = {"SEGURO": "✅", "RIESGO_ALTO": "⚠️", "VETADO": "🛑"}[pin["level"]]

        msg = (
            "```\n"
            "═══════════════════════════════════════════\n"
            "📌 RADAR DE MERCADO — NUEVA YORK\n"
            "═══════════════════════════════════════════\n\n"
            f"⏰ HORA NY     : {ny_time}\n"
            f"📊 MERCADO     : {open_str}\n"
            f"⏳ CIERRE      : {countdown}\n"
            f"   {bar}\n\n"
            "───────────────────────────────────────────\n"
            f"🛡️  PIN SEGURIDAD : {pin_emoji} {pin['level']}\n"
            f"   Razón         : {pin['reason']}\n\n"
            "───────────────────────────────────────────\n"
            f"🌍 FILTRO MACRO  : {'✅ OK' if macro_auth else '🛑 BLOQUEADO'}\n"
            f"   {macro_reason}\n"
        )
        if next_ev:
            msg += f"\n⏰ Próximo evento : {next_ev.get('time','?')} — {next_ev.get('event','?')} ({next_ev.get('impact','?')})\n"
        msg += "═══════════════════════════════════════════\n```"

        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_macro(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        report = self.macro.format_report()
        chunks = [report[i:i+4000] for i in range(0, len(report), 4000)]
        for chunk in chunks:
            await update.message.reply_text(f"```\n{chunk}\n```", parse_mode="Markdown")

    async def cmd_forense(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        report = await self.forense.generar_reporte()
        chunks = [report[i:i+4000] for i in range(0, len(report), 4000)]
        for chunk in chunks:
            await update.message.reply_text(f"```\n{chunk}\n```", parse_mode="Markdown")

    async def cmd_ayuda(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.cmd_start(update, context)

    # ─── CONVERSATIONAL HANDLER ───────────────────────────────────────────────

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.lower()

        # Map common Spanish phrases to commands
        if any(w in text for w in ["status", "estado", "puedo operar", "mercado", "sesion", "sesión", "hora"]):
            await self.cmd_status(update, context)
            return
        if any(w in text for w in ["forense", "perdida", "pérdida", "errores", "pérdidas", "perdidas"]):
            await self.cmd_forense(update, context)
            return
        if any(w in text for w in ["macro", "evento", "calendario", "noticias", "nfp", "cpi"]):
            await self.cmd_macro(update, context)
            return

        # Conversational response via Groq
        await update.message.reply_text("🧠 Consultando IA...")
        try:
            pin = self.session.get_risk_pin()
            macro_auth, macro_reason = self.macro.authorize_trade()
            system_ctx = (
                f"Eres el asistente de trading del sistema Cerebro Quant v5. "
                f"Estado actual del mercado: PIN={pin['level']}, "
                f"Filtro macro: {'OK' if macro_auth else macro_reason}. "
                f"Mercado {'abierto' if self.session.is_market_open() else 'cerrado'}. "
                f"Responde en español, de forma concisa y profesional."
            )
            client = AsyncGroq(api_key=self.groq_key)
            response = await client.chat.completions.create(
                model=self.groq_model,
                messages=[
                    {"role": "system", "content": system_ctx},
                    {"role": "user", "content": update.message.text}
                ],
                max_tokens=400
            )
            reply = response.choices[0].message.content.strip()
            await update.message.reply_text(reply)
        except Exception as e:
            logger.error(f"Groq error: {e}")
            await update.message.reply_text(f"⚠️ Error al consultar IA: {e}")

    # ─── ALERT SENDER ─────────────────────────────────────────────────────────

    async def send_alert(self, message: str) -> None:
        """Send an alert to the configured chat_id. Safe to call even if bot is disabled."""
        if self.application is None or not self.chat_id:
            logger.debug("TelegramBot.send_alert: bot not configured, skipping")
            return
        try:
            await self.application.bot.send_message(
                chat_id=self.chat_id,
                text=f"🚨 ALERTA\n\n{message}",
            )
        except Exception as e:
            logger.error("Error enviando alerta Telegram: %s", e)

    # ─── START POLLING ────────────────────────────────────────────────────────

    async def run_polling(self) -> None:
        """Start polling. If bot is not configured, keeps the coroutine alive silently."""
        if self.application is None:
            logger.warning("TelegramBot: polling skipped (not configured)")
            while True:
                await asyncio.sleep(60)
            return

        logger.info("TelegramBot: starting polling...")
        try:
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling(drop_pending_updates=True)
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("TelegramBot: polling cancelled")
        except Exception as e:
            logger.error("TelegramBot.run_polling error: %s", e)
        finally:
            try:
                if self.application.updater.running:
                    await self.application.updater.stop()
                await self.application.stop()
                await self.application.shutdown()
            except Exception as shutdown_exc:
                logger.error("TelegramBot shutdown error: %s", shutdown_exc)
