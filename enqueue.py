#!/usr/bin/env python3
"""ColdEmailMax — enqueue step (CLI).

Usage:
    python enqueue.py "apollo-contacts-export (1).csv" [YYYY-MM-DD]

Reads an Apollo OR lead-list CSV, researches each company (website + Gemini),
fills in the email template, and queues every email in queue.json with a
send date (default: next business day) for the 9:00 AM ET sender.
The web app (app.py) does the same thing with a UI.
"""

import sys
import time
from datetime import datetime
from pathlib import Path

import core


def main():
    if len(sys.argv) < 2:
        sys.exit('usage: python enqueue.py "export.csv" [YYYY-MM-DD]')
    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        sys.exit(f"file not found: {csv_path}")
    send_date = sys.argv[2] if len(sys.argv) > 2 else core.next_weekday(1)

    core.load_env()
    providers = core.active_providers()
    if not providers:
        sys.exit("No LLM provider configured — set GEMINI_API_KEY (and/or GROQ_API_KEY) in .env")
    print("providers:", ", ".join(p["name"] for p in providers))

    template = core.TEMPLATE_FILE.read_text()
    contacts = core.parse_csv(csv_path.read_text(encoding="utf-8-sig"))
    queue = core.load_queue()
    already = {e["to"].lower() for e in queue}

    print(f"{len(contacts)} contacts — queueing for {send_date} 9:00 AM ET\n")

    company_lines: dict[str, str] = {}
    site_cache: dict[str, str] = {}
    added = 0

    for c in contacts:
        if c["email"] in already:
            print(f"  - {c['email']} already queued, skipping")
            continue
        print(f"  * {c['first_name']} @ {c['company']} <{c['email']}>")
        if c["company"] not in company_lines:
            if c["website"] not in site_cache:
                site_cache[c["website"]] = core.fetch_site_text(c["website"])
            line, provider = core.generate_line(c, site_cache[c["website"]])
            company_lines[c["company"]] = line
            print(f"      [{provider}] {line}")
            time.sleep(2)

        subject, body = core.build_email(template, c, company_lines[c["company"]])
        queue.append(
            {
                "to": c["email"],
                "first_name": c["first_name"],
                "company": c["company"],
                "subject": subject,
                "body": body,
                "send_date": send_date,
                "status": "pending",
                "queued_at": datetime.now(core.ET).isoformat(),
            }
        )
        already.add(c["email"])
        added += 1
        core.save_queue(queue)

    print(f"\nQueued {added} emails -> queue.json")


if __name__ == "__main__":
    main()
