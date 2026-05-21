#!/usr/bin/env python3
"""Cerebro Quant v5 — Entry Point"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config_alpaca.json"

# Import all modules
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
        print(f"Config not found: {CONFIG_FILE}")
        print("   Copy config_template.json to config_alpaca.json and fill in your credentials")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    config = load_config()
    base_dir = str(BASE_DIR)

    # Inject base_dir into config so modules can resolve brain/ paths
    config["base_dir"] = base_dir

    # Initialize components
    session = SessionManager()
    price_feed = PriceFeed(config)

    # Setup telegram bot first so alert callback is available
    bot = TelegramBot.__new__(TelegramBot)  # will init below after veto

    macro = MacroFilter(config, telegram_alert_fn=None)   # callback wired after bot init
    forense = ForenseLogger(base_dir, telegram_alert_fn=None)
    whale = WhaleTracker(config, base_dir=base_dir, telegram_alert_fn=None)
    veto = VetoEngine(session, macro, forense, whale)
    trading = TradingEngine(config, veto, price_feed, forense)

    bot.__init__(config, session, macro, forense, veto)

    # Wire up alert callbacks
    macro.telegram_alert_fn = bot.send_alert
    forense.telegram_alert_fn = bot.send_alert
    whale.telegram_alert_fn = bot.send_alert

    dashboard = Dashboard(session, macro, forense, whale, veto)

    symbols = config.get("trading_params", {}).get("simbolos", ["SPY", "QQQ"])

    # Pre-load macro calendar
    try:
        await macro.load_calendar_finnhub()
    except Exception as e:
        logging.warning("Initial macro calendar load failed: %s", e)

    logging.info("Cerebro Quant v5 starting. Symbols: %s", symbols)

    # Run all loops concurrently
    await asyncio.gather(
        dashboard.run(),
        macro.monitor_loop(),
        whale.run_loop(symbols),
        trading.scan_and_trade(symbols),
        bot.run_polling(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCerebro Quant v5 detenido")
