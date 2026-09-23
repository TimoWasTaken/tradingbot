"""Make a licence key for a buyer of the Blocket Deal Finder desktop edition.

    py tools/keygen.py buyer@example.com [more@example.com ...]

Reads "license_secret" from secrets.json (the same secret build_app.bat bakes into the .exe).
Send the buyer the e-mail they used and the key; they enter both under Settings > Licence."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from licensing import make_key  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    secrets = json.loads((ROOT / "secrets.json").read_text(encoding="utf-8"))
    secret = secrets.get("license_secret", "")
    if not secret or "PASTE" in secret.upper():
        print("Put a long random string in secrets.json as \"license_secret\" first, then rebuild the .exe with build_app.bat.")
        return 1
    for email in sys.argv[1:]:
        print(f"{email.strip().lower()}  ->  {make_key(email, secret)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
