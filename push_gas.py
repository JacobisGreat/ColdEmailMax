#!/usr/bin/env python3
"""Push pending queue.json entries to the Google Apps Script web app."""

import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).parent


def load_env():
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main():
    load_env()
    url = os.environ.get("GAS_WEB_APP_URL")
    token = os.environ.get("GAS_TOKEN")
    if not url or not token:
        sys.exit("GAS_WEB_APP_URL / GAS_TOKEN missing from .env")

    queue = json.loads((ROOT / "queue.json").read_text())
    pending = [e for e in queue if e["status"] == "pending"]
    if not pending:
        print("No pending entries to push.")
        return

    resp = requests.post(
        url,
        json={"token": token, "entries": pending},
        timeout=60,
        allow_redirects=True,  # Apps Script replies via a 302 redirect
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        sys.exit(f"Apps Script rejected the push: {data}")
    print(f"Pushed {data['added']} new entries (Apps Script now holds {data['total']}).")
    print("They'll go out at 9:00 AM ET on their send date — laptop can be off.")


if __name__ == "__main__":
    main()
