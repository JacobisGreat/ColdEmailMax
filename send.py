#!/usr/bin/env python3
"""ColdEmailMax — sender. Runs in GitHub Actions at 9:00 AM America/New_York.

Sends every queue.json entry whose send_date is today (or earlier) and whose
status is pending, via Gmail SMTP. Marks entries sent in place.

Env vars (GitHub repo secrets): GMAIL_ADDRESS, GMAIL_APP_PASSWORD
Flags: --force (skip the 9 AM ET hour guard, for manual runs/tests)
"""

import json
import os
import random
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
QUEUE_FILE = ROOT / "queue.json"
ET = ZoneInfo("America/New_York")

FROM_NAME = "Jacob"


def main():
    force = "--force" in sys.argv
    now = datetime.now(ET)

    # Two UTC crons fire (13:00 & 14:00) so one of them is always 9 AM ET
    # regardless of daylight saving. The wrong one exits here.
    if not force and now.hour != 9:
        print(f"Not 9 AM ET (it's {now:%H:%M %Z}) — exiting.")
        return

    address = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not address or not password:
        sys.exit("GMAIL_ADDRESS / GMAIL_APP_PASSWORD env vars missing")

    if not QUEUE_FILE.exists():
        print("No queue.json — nothing to send.")
        return

    queue = json.loads(QUEUE_FILE.read_text())
    today = now.strftime("%Y-%m-%d")
    due = [e for e in queue if e["status"] == "pending" and e["send_date"] <= today]

    if not due:
        print("Nothing due today.")
        return

    print(f"{len(due)} emails due — sending as {address}")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(address, password)
        for i, entry in enumerate(due):
            msg = EmailMessage()
            msg["From"] = f"{FROM_NAME} <{address}>"
            msg["To"] = entry["to"]
            msg["Subject"] = entry["subject"]
            msg.set_content(entry["body"])
            try:
                smtp.send_message(msg)
                entry["status"] = "sent"
                entry["sent_at"] = datetime.now(ET).isoformat()
                print(f"  sent -> {entry['to']} ({entry['company']})")
            except smtplib.SMTPException as e:
                entry["status"] = "error"
                entry["error"] = str(e)
                print(f"  ERROR -> {entry['to']}: {e}")
            QUEUE_FILE.write_text(json.dumps(queue, indent=2))
            if i < len(due) - 1:
                time.sleep(random.randint(20, 45))  # human-ish spacing

    sent = sum(1 for e in due if e["status"] == "sent")
    errors = sum(1 for e in due if e["status"] == "error")
    print(f"\nDone: {sent} sent, {errors} errors.")


if __name__ == "__main__":
    main()
