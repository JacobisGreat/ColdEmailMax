#!/usr/bin/env python3
"""Re-render every pending queue entry with the CURRENT email.txt template.

Strategy: extract the saved personalized line out of each entry's body,
then re-run core.build_email with the latest template. This automatically
picks up sign-off, footer, age, or any other template changes — and keeps
the per-company line that was already paid for via Gemini/Groq.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import core  # noqa: E402


def extract_company_line(body: str) -> str | None:
    """The personalized line is the paragraph between "...shipped this year."
    and "I move fast..."  Find and return it (or None if structure changed)."""
    m = re.search(
        r"shipped this year\.\s*\n+\s*(.+?)\s*\n+\s*I move fast",
        body,
        re.S,
    )
    return m.group(1).strip() if m else None


def main():
    core.load_env()
    queue = core.load_queue()

    # Step 1: pull cloud snapshot to learn which entries already sent.
    cloud = core.fetch_gas_queue()
    cloud_status = {}
    if cloud.get("ok"):
        cloud_status = {e["to"].lower(): e for e in cloud.get("queue", [])}
        print(f"cloud holds {len(cloud_status)} entries")
    else:
        print(f"WARNING: cloud read failed: {cloud.get('error')}")

    # Step 2: reconcile local statuses with cloud truth.
    synced = 0
    for entry in queue:
        if entry.get("status") != "pending":
            continue
        c = cloud_status.get(entry["to"].lower())
        if c and c.get("status") == "sent":
            entry["status"] = "sent"
            entry["sent_at"] = c.get("sent_at") or entry.get("queued_at")
            synced += 1
    if synced:
        print(f"synced {synced} stale-pending local entries -> sent")

    pending = [e for e in queue if e.get("status") == "pending"]
    print(f"local entries still pending: {len(pending)}")

    # Step 3: re-render each pending entry from the current template.
    template = core.TEMPLATE_FILE.read_text()
    rebuilt = 0
    skipped = []
    for entry in pending:
        line = extract_company_line(entry.get("body", ""))
        if not line:
            skipped.append(entry["to"])
            continue
        contact = {
            "first_name": entry.get("first_name", ""),
            "title": "",
            "company": entry.get("company", ""),
            "email": entry.get("to", ""),
            "industry": "",
            "keywords": "",
            "website": "",
            "funding": "",
        }
        subject, raw = core.build_email(template, contact, line)
        entry["subject"] = subject
        entry["body"] = core.to_plain_body(raw)
        entry["html"] = core.to_html_body(raw)
        rebuilt += 1

    core.save_queue(queue)
    try:
        core.encrypt_queue()
    except Exception:
        pass
    print(f"rebuilt {rebuilt} pending entries with the new template")
    if skipped:
        print(f"WARNING: {len(skipped)} entries had non-standard structure and were untouched:")
        for e in skipped[:5]:
            print(f"  - {e}")

    # Step 4: re-push every still-pending entry to cloud (delete + add).
    pushed = 0
    failed = 0
    for entry in pending:
        try:
            core.delete_from_gas(entry["to"])
            r = core.push_to_gas([entry])
            if r.get("ok"):
                pushed += 1
            else:
                failed += 1
                print(f"  ! cloud push failed for {entry['to']}: {r.get('error')}")
        except Exception as e:
            failed += 1
            print(f"  ! exception for {entry['to']}: {e}")
    print(f"\ncloud sync: {pushed} pushed, {failed} failed")


if __name__ == "__main__":
    main()
