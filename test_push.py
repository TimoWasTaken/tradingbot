"""Sends a test notification to your phone via ntfy, using the private topic in secrets.json.

    py test_push.py
"""
from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot.config import load_config, load_secrets
from bot.notify import Notifier


def main() -> int:
    cfg = load_config()
    secrets = load_secrets()
    n = Notifier(cfg.get("ntfy", {}).get("server", "https://ntfy.sh"), secrets.get("ntfy_topic", ""), print)
    if not n.enabled:
        print("No topic configured. Copy secrets.example.json to secrets.json and put your secret topic name in \"ntfy_topic\".")
        return 1
    ok = n.send("Test from Tradingbot", "Hi! If you can read this, push notifications work.", tags=["tada"])
    print("Sent, check your phone." if ok else "Sending failed. Check your internet connection and the topic name.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
