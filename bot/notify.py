"""Push notifications via ntfy (https://ntfy.sh).

Two topics: a private one (everything: trades, signals, risk events, daily reports; the name lives in secrets.json)
and an optional public one (only trades, signals and fund decisions, always with a disclaimer footer)."""
from __future__ import annotations

import requests

PUBLIC_FOOTER = ("\n\n— Automated rule-based signal from a PAPER-TRADING bot (no real money). Not investment advice. "
                 "Past results do not predict future results. The author may hold the assets mentioned.")


class Notifier:
    def __init__(self, server: str = "https://ntfy.sh", topic: str = "", log=None,
                 public_topic: str = "", public_url: str = ""):
        self.server = (server or "https://ntfy.sh").rstrip("/")
        self.topic = (topic or "").strip()
        self.public_topic = (public_topic or "").strip()
        self.public_url = (public_url or "").strip()
        self.log = log

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    @property
    def public_enabled(self) -> bool:
        return bool(self.public_topic)

    def _post(self, topic: str, title: str, message: str, priority: int, tags) -> bool:
        try:
            r = requests.post(self.server, json={
                "topic": topic, "title": title, "message": message,
                "priority": int(priority), "tags": list(tags or []),
            }, timeout=10)
            r.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001 - a notification must never crash the bot
            if self.log:
                self.log(f"Could not send push notification: {e}")
            return False

    def send(self, title: str, message: str, priority: int = 3, tags=None) -> bool:
        """Private topic."""
        if not self.enabled:
            return False
        return self._post(self.topic, title, message, priority, tags)

    def send_public(self, title: str, message: str, tags=None) -> bool:
        """Public topic, always with the disclaimer footer."""
        if not self.public_enabled:
            return False
        footer = PUBLIC_FOOTER + (f" Rules & track record: {self.public_url}" if self.public_url else "")
        return self._post(self.public_topic, title, message + footer, 3, tags)

    def send_both(self, title: str, message: str, tags=None, priority: int = 3) -> None:
        self.send(title, message, priority, tags)
        self.send_public(title, message, tags)
