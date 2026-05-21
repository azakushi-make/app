"""
Cerebro Quant v5 — Whale Tracker
Monitors institutional sentiment via Finnhub and triggers veto signals on
strongly bearish institutional positioning.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import aiofiles
import aiohttp

logger = logging.getLogger(__name__)


class WhaleTracker:
    """
    Tracks institutional / whale sentiment per symbol.
    Uses Finnhub institutional-ownership endpoint; falls back to a neutral-positive
    simulated value when the API is unavailable.
    """

    def __init__(
        self,
        config: dict,
        base_dir: Optional[str] = None,
        telegram_alert_fn: Optional[Callable] = None,
    ) -> None:
        self.config = config
        self.telegram_alert_fn = telegram_alert_fn
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

        md = config.get("market_data", {})
        # Support both field name variants used across config versions
        self.finnhub_key: str = md.get("finnhub_api_key", md.get("finnhub_key", ""))

        # base_dir can be supplied directly or inferred from config
        if base_dir is not None:
            _base = Path(base_dir)
        else:
            _base = Path(config.get("base_dir", "."))
        brain_dir = _base / "brain"
        brain_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = brain_dir / "whale_flow.json"

        # In-memory state: {symbol: sentiment_float}
        self._state: dict = {}

    # ──────────────────────────────────────────────────────────────────────────
    # HTTP session
    # ──────────────────────────────────────────────────────────────────────────

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=12)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    # ──────────────────────────────────────────────────────────────────────────
    # Sentiment sources
    # ──────────────────────────────────────────────────────────────────────────

    async def get_institutional_sentiment_finnhub(self, symbol: str) -> float:
        """
        Fetch institutional ownership sentiment from Finnhub.
        Computes a ratio based on positive vs negative position changes.
        Returns float in [-1, +1], or 0.0 on any error.
        """
        if not self.finnhub_key:
            logger.debug("WhaleTracker: no Finnhub key, skipping institutional sentiment")
            return 0.0

        try:
            url = (
                f"https://finnhub.io/api/v1/stock/institutional-ownership"
                f"?symbol={symbol}&token={self.finnhub_key}"
            )
            session = await self._get_http_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(
                        "WhaleTracker Finnhub HTTP %s for %s", resp.status, symbol
                    )
                    return 0.0
                data = await resp.json()

            ownership_list = data.get("ownership", [])
            if not isinstance(ownership_list, list) or not ownership_list:
                logger.debug("WhaleTracker: no institutional data for %s", symbol)
                return 0.0

            positive_shares = 0.0
            negative_shares = 0.0

            for entry in ownership_list:
                try:
                    change = float(entry.get("change", 0) or 0)
                    shares = abs(float(entry.get("share", 0) or 0))
                    if change > 0:
                        positive_shares += shares
                    elif change < 0:
                        negative_shares += shares
                except (ValueError, TypeError):
                    continue

            total = positive_shares + negative_shares
            if total == 0:
                return 0.0

            # Ratio: +1 = all buying, -1 = all selling
            ratio = (positive_shares - negative_shares) / total
            ratio = max(-1.0, min(1.0, ratio))
            logger.info("WhaleTracker: %s institutional sentiment = %.3f", symbol, ratio)
            return ratio

        except aiohttp.ClientError as exc:
            logger.error("WhaleTracker Finnhub network error for %s: %s", symbol, exc)
            return 0.0
        except Exception as exc:
            logger.error("WhaleTracker unexpected error for %s: %s", symbol, exc)
            return 0.0

    async def get_simulated_sentiment(self, symbol: str) -> float:
        """
        Fallback sentiment: returns a neutral-positive constant (not random).
        This avoids introducing randomness into the veto logic.
        """
        logger.debug("WhaleTracker: using simulated sentiment for %s", symbol)
        return 0.1

    async def get_sentiment(self, symbol: str) -> float:
        """
        Try Finnhub institutional sentiment; fall back to simulated if it fails
        or returns exactly 0.0 with no key configured.
        """
        try:
            if not self.finnhub_key:
                return await self.get_simulated_sentiment(symbol)

            sentiment = await self.get_institutional_sentiment_finnhub(symbol)
            return sentiment

        except Exception as exc:
            logger.error("WhaleTracker.get_sentiment error for %s: %s — using simulated", symbol, exc)
            return await self.get_simulated_sentiment(symbol)

    # ──────────────────────────────────────────────────────────────────────────
    # Veto logic
    # ──────────────────────────────────────────────────────────────────────────

    def should_veto_buy(self, sentiment: float) -> bool:
        """
        Return True (veto a BUY) when institutional sentiment is strongly bearish.
        Threshold: sentiment < -0.7
        """
        return sentiment < -0.7

    # ──────────────────────────────────────────────────────────────────────────
    # State persistence
    # ──────────────────────────────────────────────────────────────────────────

    async def save_state(self, data: dict) -> None:
        """Save whale flow state to JSON with asyncio lock."""
        async with self._lock:
            try:
                payload = {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "sentiments": data,
                }
                content = json.dumps(payload, indent=2, ensure_ascii=False)
                async with aiofiles.open(self.file_path, "w", encoding="utf-8") as fh:
                    await fh.write(content)
                logger.debug("WhaleTracker: state saved for %d symbols", len(data))
            except (IOError, TypeError) as exc:
                logger.error("WhaleTracker.save_state error: %s", exc)
            except Exception as exc:
                logger.error("WhaleTracker.save_state unexpected error: %s", exc)

    async def load_state(self) -> dict:
        """Load whale flow state from JSON. Returns {} if missing or corrupt."""
        try:
            if not self.file_path.exists():
                return {}
            async with aiofiles.open(self.file_path, "r", encoding="utf-8") as fh:
                content = await fh.read()
            if not content.strip():
                return {}
            payload = json.loads(content)
            return payload.get("sentiments", {})
        except (json.JSONDecodeError, IOError) as exc:
            logger.error("WhaleTracker.load_state error: %s", exc)
            return {}
        except Exception as exc:
            logger.error("WhaleTracker.load_state unexpected error: %s", exc)
            return {}

    # ──────────────────────────────────────────────────────────────────────────
    # Background monitoring loop
    # ──────────────────────────────────────────────────────────────────────────

    async def run_loop(self, symbols: list) -> None:
        """
        Every 60 seconds: fetch sentiment for each symbol, save state, log.
        Also sends Telegram alerts for strongly bearish sentiment changes.
        """
        logger.info("WhaleTracker: run_loop started for symbols %s", symbols)

        while True:
            try:
                new_state: dict = {}

                for symbol in symbols:
                    try:
                        sentiment = await self.get_sentiment(symbol)
                        new_state[symbol] = round(sentiment, 4)

                        if self.should_veto_buy(sentiment):
                            msg = (
                                f"🐋 WHALE ALERT — {symbol}\n"
                                f"Sentimiento institucional: {sentiment:.3f}\n"
                                f"BUY vetado por posicionamiento bajista."
                            )
                            logger.warning("WhaleTracker: %s", msg.replace("\n", " | "))
                            if self.telegram_alert_fn is not None:
                                try:
                                    await self.telegram_alert_fn(msg)
                                except Exception as alert_exc:
                                    logger.error(
                                        "WhaleTracker: telegram alert failed: %s", alert_exc
                                    )

                    except Exception as sym_exc:
                        logger.error(
                            "WhaleTracker.run_loop symbol %s error: %s", symbol, sym_exc
                        )
                        new_state[symbol] = 0.0

                self._state = new_state
                await self.save_state(new_state)

                logger.info("WhaleTracker: updated sentiments: %s", new_state)

            except Exception as exc:
                logger.error("WhaleTracker.run_loop iteration error: %s", exc)

            await asyncio.sleep(60)

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
