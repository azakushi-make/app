"""
Cerebro Quant v5 — Price Feed
Fetches real-time prices from multiple providers with parallel fallback.
"""

import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class PriceFeed:
    """Multi-source price feed with parallel fetching and automatic fallback."""

    def __init__(self, config: dict) -> None:
        self.config = config
        md = config.get("market_data", {})
        # Support both underscore and plain key variants across config versions
        self.twelvedata_key: str = md.get("twelvedata_api_key", md.get("twelvedata_key", ""))
        self.finnhub_key: str = md.get("finnhub_api_key", md.get("finnhub_key", ""))
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create and reuse an aiohttp session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def get_price_twelvedata(self, symbol: str) -> Optional[float]:
        """Fetch current price from Twelve Data API."""
        if not self.twelvedata_key:
            return None
        try:
            url = f"https://api.twelvedata.com/price?symbol={symbol}&apikey={self.twelvedata_key}"
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("TwelveData HTTP %s for %s", resp.status, symbol)
                    return None
                data = await resp.json()
                price_str = data.get("price")
                if price_str is None:
                    logger.warning("TwelveData: no 'price' field for %s: %s", symbol, data)
                    return None
                price = float(price_str)
                logger.debug("TwelveData %s = %.4f", symbol, price)
                return price
        except (aiohttp.ClientError, ValueError, KeyError) as exc:
            logger.error("TwelveData error for %s: %s", symbol, exc)
            return None
        except Exception as exc:
            logger.error("TwelveData unexpected error for %s: %s", symbol, exc)
            return None

    async def get_price_finnhub(self, symbol: str) -> Optional[float]:
        """Fetch current price from Finnhub API (field 'c' = current price)."""
        if not self.finnhub_key:
            return None
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={self.finnhub_key}"
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Finnhub HTTP %s for %s", resp.status, symbol)
                    return None
                data = await resp.json()
                price = data.get("c")
                if price is None or price == 0:
                    logger.warning("Finnhub: invalid price for %s: %s", symbol, data)
                    return None
                price = float(price)
                logger.debug("Finnhub %s = %.4f", symbol, price)
                return price
        except (aiohttp.ClientError, ValueError, KeyError) as exc:
            logger.error("Finnhub error for %s: %s", symbol, exc)
            return None
        except Exception as exc:
            logger.error("Finnhub unexpected error for %s: %s", symbol, exc)
            return None

    async def get_price_yfinance(self, symbol: str) -> Optional[float]:
        """Fetch current price from yfinance in a thread executor to avoid blocking."""
        try:
            loop = asyncio.get_running_loop()

            def _fetch() -> Optional[float]:
                try:
                    import yfinance as yf
                    ticker = yf.Ticker(symbol)
                    price = ticker.fast_info.last_price
                    if price is None or price != price:  # NaN check
                        return None
                    return float(price)
                except Exception as inner_exc:
                    logger.error("yfinance inner error for %s: %s", symbol, inner_exc)
                    return None

            price = await loop.run_in_executor(None, _fetch)
            if price is not None:
                logger.debug("yfinance %s = %.4f", symbol, price)
            return price
        except Exception as exc:
            logger.error("yfinance executor error for %s: %s", symbol, exc)
            return None

    async def get_price(self, symbol: str) -> float:
        """
        Fetch price using all 3 sources in parallel.
        Returns the average of all successful responses.
        Falls back to the single available source if others fail.
        Raises RuntimeError if no source returns a valid price.
        """
        try:
            results = await asyncio.gather(
                self.get_price_twelvedata(symbol),
                self.get_price_finnhub(symbol),
                self.get_price_yfinance(symbol),
                return_exceptions=True,
            )

            valid_prices = []
            source_names = ["TwelveData", "Finnhub", "yfinance"]
            for idx, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error("Source %s raised exception for %s: %s", source_names[idx], symbol, result)
                elif result is not None:
                    valid_prices.append(result)

            if not valid_prices:
                raise RuntimeError(f"All price sources failed for {symbol}")

            avg_price = sum(valid_prices) / len(valid_prices)
            logger.info(
                "Price %s = %.4f (avg of %d sources: %s)",
                symbol,
                avg_price,
                len(valid_prices),
                [f"{p:.4f}" for p in valid_prices],
            )
            return avg_price

        except RuntimeError:
            raise
        except Exception as exc:
            logger.error("get_price unexpected error for %s: %s", symbol, exc)
            raise RuntimeError(f"Price fetch failed for {symbol}: {exc}") from exc

    async def get_prices_bulk(self, symbols: list) -> dict:
        """
        Fetch prices for all symbols concurrently.
        Returns dict {symbol: price}. Missing symbols are omitted on error.
        """
        tasks = {symbol: self.get_price(symbol) for symbol in symbols}
        prices = {}

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for symbol, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error("Bulk price fetch failed for %s: %s", symbol, result)
            else:
                prices[symbol] = result

        logger.info("Bulk prices fetched: %d/%d symbols", len(prices), len(symbols))
        return prices

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
