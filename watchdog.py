#!/usr/bin/env python3
"""
Cerebro Quant v5 — Watchdog
Monitors the main bot process and restarts it automatically on crash.

Usage:
    python watchdog.py
"""

import asyncio
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

LOGS_DIR = Path(__file__).parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "watchdog.log"),
    ],
)
logger = logging.getLogger("Watchdog")

SCRIPT    = Path(__file__).parent / "cerebro_quant_v5.py"
MAX_RESTARTS   = 20      # stop watchdog after this many crashes in a row
COOLDOWN_BASE  = 5       # seconds to wait after first crash
COOLDOWN_MAX   = 300     # cap backoff at 5 minutes


async def run_bot(restart_count: int) -> int:
    """Launch the bot and wait for it to exit. Returns the exit code."""
    cmd = [sys.executable, str(SCRIPT)]
    logger.info("Iniciando bot (intento #%d): %s", restart_count + 1, " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=None,   # inherit — let bot output go to terminal
        stderr=None,
    )
    exit_code = await proc.wait()
    return exit_code


async def watchdog_loop() -> None:
    restart_count = 0
    cooldown = COOLDOWN_BASE

    while True:
        exit_code = await run_bot(restart_count)

        if exit_code == 0:
            logger.info("Bot terminó limpiamente (código 0). Watchdog finalizado.")
            break

        restart_count += 1
        logger.warning(
            "Bot crasheó con código %d (crash #%d). Reiniciando en %ds...",
            exit_code, restart_count, cooldown,
        )

        if restart_count >= MAX_RESTARTS:
            logger.error(
                "Se alcanzó el límite de %d reinicios. "
                "Watchdog detenido — revisa los logs manualmente.",
                MAX_RESTARTS,
            )
            break

        await asyncio.sleep(cooldown)
        # Exponential backoff, capped
        cooldown = min(cooldown * 2, COOLDOWN_MAX)


if __name__ == "__main__":
    if not SCRIPT.exists():
        print(f"❌ No se encontró {SCRIPT}")
        sys.exit(1)

    logger.info("=== Watchdog Cerebro Quant v5 iniciado ===")
    try:
        asyncio.run(watchdog_loop())
    except KeyboardInterrupt:
        logger.info("Watchdog detenido por usuario (Ctrl+C)")
