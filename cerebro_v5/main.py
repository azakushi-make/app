#!/usr/bin/env python3
"""
CEREBRO QUANT v5 — Entry Point
Sistema de trading automatizado con 5 IAs, vetos cruzados y dashboard Bloomberg
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config_alpaca.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-16s] %(message)s",
    handlers=[
        logging.StreamHandler(),
    ]
)

from modules.session_manager import SessionManager
from modules.price_feed import PriceFeed
from modules.macro_filter import MacroFilter
from modules.forense import ForenseLogger
from modules.whale_tracker import WhaleTracker
from modules.veto_engine import VetoEngine
from modules.trading_engine import TradingEngine
from modules.telegram_bot import TelegramBot
from modules.dashboard import Dashboard


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"❌ No se encontró: {CONFIG_FILE}")
        print("   Copia config_template.json → config_alpaca.json y rellena tus credenciales")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


async def main():
    config = load_config()
    base_dir = str(BASE_DIR)
    symbols = config.get("trading_params", {}).get("simbolos", ["SPY", "QQQ"])

    print("🧠 Iniciando Cerebro Quant v5...")

    # ── Inicializar módulos ──────────────────────────────────────────────────
    session  = SessionManager()
    price_feed = PriceFeed(config)
    macro    = MacroFilter(config, telegram_alert_fn=None)
    forense  = ForenseLogger(base_dir, telegram_alert_fn=None)
    whale    = WhaleTracker(config, base_dir, telegram_alert_fn=None)
    veto     = VetoEngine(session, macro, forense, whale)
    trading  = TradingEngine(config, veto, price_feed, forense)
    bot      = TelegramBot(config, session, macro, forense, veto)
    dashboard = Dashboard(session, macro, forense, whale, veto)

    # ── Cablear callbacks de alerta ──────────────────────────────────────────
    macro.telegram_alert_fn   = bot.send_alert
    forense.telegram_alert_fn = bot.send_alert
    whale.telegram_alert_fn   = bot.send_alert

    # ── Conectar señales al dashboard ────────────────────────────────────────
    trading.on_signal = lambda sig: dashboard.signals.append(sig)
    trading.on_log    = lambda msg: dashboard.push_log(msg)

    # ── Cargar calendario macro inicial ──────────────────────────────────────
    await macro.load_calendar_finnhub()

    print(f"✅ Sistema listo. Activos: {symbols}")
    print("   Telegram, dashboard y motores de IA iniciando...")

    # ── Correr todos los loops concurrentemente ──────────────────────────────
    tasks = [
        asyncio.create_task(dashboard.run(),          name="dashboard"),
        asyncio.create_task(macro.monitor_loop(),     name="macro_monitor"),
        asyncio.create_task(whale.run_loop(symbols),  name="whale_tracker"),
        asyncio.create_task(trading.scan_and_trade(symbols), name="trading_engine"),
        asyncio.create_task(bot.run_polling(),        name="telegram_bot"),
    ]

    try:
        await asyncio.gather(*tasks, return_exceptions=False)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.error(f"Error crítico: {e}")
    finally:
        for t in tasks:
            t.cancel()
        print("\n💤 Cerebro Quant v5 detenido")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n💤 Hasta luego")
