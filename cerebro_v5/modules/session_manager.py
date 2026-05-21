"""
Cerebro Quant v5 — Session Manager
Tracks NY market session state, risk windows, and provides formatted outputs.
"""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages New York market session state and risk levels."""

    tz = ZoneInfo("America/New_York")
    NY_OPEN = time(9, 30)
    NY_CLOSE = time(16, 0)

    # Session duration: 9:30 to 16:00 = 6.5 hours = 390 minutes
    SESSION_DURATION_SECONDS = 6 * 3600 + 30 * 60  # 23400 seconds

    def get_ny_now(self) -> datetime:
        """Return current datetime in New York timezone."""
        return datetime.now(tz=self.tz)

    def is_market_open(self) -> bool:
        """Return True if the market is currently open (weekday 0-4, 9:30-16:00 NY)."""
        try:
            now = self.get_ny_now()
            if now.weekday() > 4:
                return False
            current_time = now.time()
            return self.NY_OPEN <= current_time < self.NY_CLOSE
        except Exception as exc:
            logger.error("is_market_open error: %s", exc)
            return False

    def get_countdown_seconds(self) -> int:
        """Return seconds remaining until 16:00 close. Returns 0 if market is closed."""
        try:
            if not self.is_market_open():
                return 0
            now = self.get_ny_now()
            close_today = now.replace(
                hour=self.NY_CLOSE.hour,
                minute=self.NY_CLOSE.minute,
                second=0,
                microsecond=0,
            )
            delta = int((close_today - now).total_seconds())
            return max(0, delta)
        except Exception as exc:
            logger.error("get_countdown_seconds error: %s", exc)
            return 0

    def get_countdown_str(self) -> str:
        """Return formatted countdown string like '03h 24m 15s'."""
        try:
            seconds = self.get_countdown_seconds()
            if seconds <= 0:
                return "00h 00m 00s"
            hours, remainder = divmod(seconds, 3600)
            minutes, secs = divmod(remainder, 60)
            return f"{hours:02d}h {minutes:02d}m {secs:02d}s"
        except Exception as exc:
            logger.error("get_countdown_str error: %s", exc)
            return "00h 00m 00s"

    def get_session_progress(self) -> float:
        """Return float 0-100 representing percentage through the 6.5-hour session."""
        try:
            if not self.is_market_open():
                return 0.0
            now = self.get_ny_now()
            open_today = now.replace(
                hour=self.NY_OPEN.hour,
                minute=self.NY_OPEN.minute,
                second=0,
                microsecond=0,
            )
            elapsed = (now - open_today).total_seconds()
            progress = (elapsed / self.SESSION_DURATION_SECONDS) * 100.0
            return round(min(100.0, max(0.0, progress)), 2)
        except Exception as exc:
            logger.error("get_session_progress error: %s", exc)
            return 0.0

    def get_progress_bar(self, width: int = 20) -> str:
        """Return ASCII progress bar like '[████████░░░░░░░░░░░░] 42%'."""
        try:
            progress = self.get_session_progress()
            filled = int((progress / 100.0) * width)
            empty = width - filled
            bar = "█" * filled + "░" * empty
            pct = int(progress)
            return f"[{bar}] {pct}%"
        except Exception as exc:
            logger.error("get_progress_bar error: %s", exc)
            return f"[{'░' * width}] 0%"

    def get_risk_pin(self) -> dict:
        """
        Return risk level dict with keys: level, reason, emoji.
        - VETADO: market closed
        - RIESGO_ALTO: first 30 min (9:30-10:00) OR last 30 min (15:30-16:00)
        - SEGURO: 10:00-15:30
        """
        try:
            now = self.get_ny_now()

            if not self.is_market_open():
                return {
                    "level": "VETADO",
                    "reason": "Mercado cerrado fuera de horario NYSE",
                    "emoji": "🔴",
                }

            current_time = now.time()

            # First 30 minutes: 9:30 to 10:00
            open_plus_30 = time(10, 0)
            # Last 30 minutes: 15:30 to 16:00
            close_minus_30 = time(15, 30)

            if current_time < open_plus_30:
                return {
                    "level": "RIESGO_ALTO",
                    "reason": "Apertura volátil (primeros 30 min)",
                    "emoji": "🟡",
                }

            if current_time >= close_minus_30:
                return {
                    "level": "RIESGO_ALTO",
                    "reason": "Cierre volátil (últimos 30 min)",
                    "emoji": "🟡",
                }

            return {
                "level": "SEGURO",
                "reason": "Ventana segura de operación 10:00-15:30",
                "emoji": "🟢",
            }

        except Exception as exc:
            logger.error("get_risk_pin error: %s", exc)
            return {
                "level": "VETADO",
                "reason": f"Error interno: {exc}",
                "emoji": "🔴",
            }
