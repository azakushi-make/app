"""
Cerebro Quant v5 — Macro Filter
Monitors high-impact economic calendar events and blocks trading during risk windows.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import aiohttp

logger = logging.getLogger(__name__)

NY_TZ = ZoneInfo("America/New_York")


class MacroFilter:
    """Filters trades around high-impact economic events from Finnhub economic calendar."""

    def __init__(self, config: dict, telegram_alert_fn: Optional[Callable] = None) -> None:
        self.config = config
        self.telegram_alert_fn = telegram_alert_fn
        self.events: list = []
        self.paused: bool = False
        self.pause_reason: str = ""
        self.window_min: int = 15
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

        md = config.get("market_data", {})
        self.finnhub_key: str = md.get("finnhub_api_key", md.get("finnhub_key", ""))

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _get_ny_now(self) -> datetime:
        return datetime.now(tz=NY_TZ)

    async def load_calendar_finnhub(self) -> None:
        """
        Load high-impact economic events from Finnhub economic calendar API.
        Falls back to _load_calendar_hardcoded() on any error.
        """
        if not self.finnhub_key:
            logger.warning("MacroFilter: no Finnhub key configured, using hardcoded calendar")
            self._load_calendar_hardcoded()
            return

        try:
            url = f"https://finnhub.io/api/v1/calendar/economic?token={self.finnhub_key}"
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Finnhub calendar HTTP %s — falling back to hardcoded", resp.status)
                    self._load_calendar_hardcoded()
                    return
                data = await resp.json()

            raw_events = data.get("economicCalendar", [])
            if not isinstance(raw_events, list):
                logger.warning("Finnhub calendar unexpected format — falling back")
                self._load_calendar_hardcoded()
                return

            now_ny = self._get_ny_now()
            today_date = now_ny.date()
            parsed = []

            for ev in raw_events:
                try:
                    impact = (ev.get("impact") or "").lower()
                    if impact != "high":
                        continue

                    time_str = ev.get("time") or ev.get("date")
                    if not time_str:
                        continue

                    # Finnhub returns times in UTC: "2024-11-01 08:30:00"
                    if " " in time_str:
                        dt_utc = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                    else:
                        dt_utc = datetime.strptime(time_str, "%Y-%m-%d")

                    dt_utc = dt_utc.replace(tzinfo=ZoneInfo("UTC"))
                    dt_ny = dt_utc.astimezone(NY_TZ)

                    if dt_ny.date() != today_date:
                        continue

                    parsed.append({
                        "name": ev.get("event", "Economic Event"),
                        "time": dt_ny,
                        "impact": "high",
                        "country": ev.get("country", "US"),
                        "actual": ev.get("actual"),
                        "estimate": ev.get("estimate"),
                        "prev": ev.get("prev"),
                    })
                except (ValueError, KeyError, TypeError) as parse_exc:
                    logger.debug("MacroFilter: skipped event parse error: %s", parse_exc)
                    continue

            async with self._lock:
                self.events = sorted(parsed, key=lambda e: e["time"])

            logger.info("MacroFilter: loaded %d high-impact events for today", len(self.events))

        except aiohttp.ClientError as exc:
            logger.error("MacroFilter Finnhub network error: %s — falling back", exc)
            self._load_calendar_hardcoded()
        except Exception as exc:
            logger.error("MacroFilter unexpected error loading calendar: %s — falling back", exc)
            self._load_calendar_hardcoded()

    def _load_calendar_hardcoded(self) -> None:
        """
        Fallback: return empty event list.
        We never hardcode dates — if the API is unavailable we operate without events.
        """
        self.events = []
        logger.info("MacroFilter: using empty hardcoded calendar (no dates hardcoded)")

    def is_blocked(self) -> bool:
        """
        Return True if current NY time is within window_min minutes of any high-impact event.
        """
        try:
            now_ny = self._get_ny_now()
            window = timedelta(minutes=self.window_min)
            for ev in self.events:
                ev_time = ev["time"]
                if not ev_time.tzinfo:
                    ev_time = ev_time.replace(tzinfo=NY_TZ)
                diff = abs((now_ny - ev_time).total_seconds())
                if diff <= window.total_seconds():
                    return True
            return False
        except Exception as exc:
            logger.error("MacroFilter.is_blocked error: %s", exc)
            return False

    def authorize_trade(self) -> tuple:
        """
        Return (True, "OK") if trading is allowed, (False, reason) otherwise.
        """
        try:
            if self.paused:
                return False, f"MacroFilter pausado manualmente: {self.pause_reason}"

            if self.is_blocked():
                now_ny = self._get_ny_now()
                window = timedelta(minutes=self.window_min)
                for ev in self.events:
                    ev_time = ev["time"]
                    if not ev_time.tzinfo:
                        ev_time = ev_time.replace(tzinfo=NY_TZ)
                    diff = abs((now_ny - ev_time).total_seconds())
                    if diff <= window.total_seconds():
                        ev_name = ev.get("name", "Evento macro")
                        ev_time_str = ev_time.strftime("%H:%M")
                        return False, f"Bloqueado por {ev_name} a las {ev_time_str} (±{self.window_min}min)"

            return True, "OK"

        except Exception as exc:
            logger.error("MacroFilter.authorize_trade error: %s", exc)
            return False, f"Error en MacroFilter: {exc}"

    def get_next_event(self) -> Optional[dict]:
        """Return the next upcoming high-impact event (None if no future events today)."""
        try:
            now_ny = self._get_ny_now()
            for ev in self.events:
                ev_time = ev["time"]
                if not ev_time.tzinfo:
                    ev_time = ev_time.replace(tzinfo=NY_TZ)
                if ev_time > now_ny:
                    return ev
            return None
        except Exception as exc:
            logger.error("MacroFilter.get_next_event error: %s", exc)
            return None

    async def monitor_loop(self) -> None:
        """
        Every 60 seconds: reload calendar and check block status.
        If state changes from unblocked→blocked, send Telegram alert.
        """
        was_blocked = False
        logger.info("MacroFilter: monitor_loop started")

        while True:
            try:
                await self.load_calendar_finnhub()
                now_blocked = self.is_blocked()

                if now_blocked and not was_blocked:
                    auth_ok, reason = self.authorize_trade()
                    msg = f"⚠️ MACRO BLOCK ACTIVADO\n{reason}\nOperaciones suspendidas."
                    logger.warning("MacroFilter: %s", msg)
                    if self.telegram_alert_fn is not None:
                        try:
                            await self.telegram_alert_fn(msg)
                        except Exception as alert_exc:
                            logger.error("MacroFilter: telegram alert failed: %s", alert_exc)

                elif was_blocked and not now_blocked:
                    msg = "✅ MACRO BLOCK LIBERADO\nOperaciones autorizadas nuevamente."
                    logger.info("MacroFilter: %s", msg)
                    if self.telegram_alert_fn is not None:
                        try:
                            await self.telegram_alert_fn(msg)
                        except Exception as alert_exc:
                            logger.error("MacroFilter: telegram alert failed: %s", alert_exc)

                was_blocked = now_blocked

            except Exception as exc:
                logger.error("MacroFilter.monitor_loop iteration error: %s", exc)

            await asyncio.sleep(60)

    def format_report(self) -> str:
        """Return a formatted report with event list and current status."""
        try:
            now_ny = self._get_ny_now()
            auth_ok, auth_reason = self.authorize_trade()
            next_ev = self.get_next_event()

            lines = [
                "═" * 50,
                "       📅  MACRO FILTER  —  REPORTE",
                "═" * 50,
                f"  Hora NY   : {now_ny.strftime('%Y-%m-%d %H:%M:%S')}",
                f"  Estado    : {'🔴 BLOQUEADO' if not auth_ok else '🟢 AUTORIZADO'}",
                f"  Razón     : {auth_reason}",
                f"  Ventana   : ±{self.window_min} minutos",
                "─" * 50,
                f"  Eventos hoy: {len(self.events)}",
            ]

            if self.events:
                lines.append("─" * 50)
                for ev in self.events:
                    ev_time = ev["time"]
                    if not ev_time.tzinfo:
                        ev_time = ev_time.replace(tzinfo=NY_TZ)
                    marker = "▶" if ev_time > now_ny else "✓"
                    lines.append(
                        f"  {marker} {ev_time.strftime('%H:%M')}  {ev.get('name', '?'):30s}  [{ev.get('country','US')}]"
                    )
            else:
                lines.append("  (Sin eventos de alto impacto para hoy)")

            if next_ev:
                ev_time = next_ev["time"]
                if not ev_time.tzinfo:
                    ev_time = ev_time.replace(tzinfo=NY_TZ)
                diff_min = int((ev_time - now_ny).total_seconds() / 60)
                lines.append("─" * 50)
                lines.append(f"  Próximo    : {next_ev.get('name','?')} en {diff_min}min")

            lines.append("═" * 50)
            return "\n".join(lines)

        except Exception as exc:
            logger.error("MacroFilter.format_report error: %s", exc)
            return f"Error generando reporte macro: {exc}"

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
