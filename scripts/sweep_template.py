#!/usr/bin/env python3
"""One-time sweep: rewrite already-queued pending emails to use the new
template (18 instead of 17, hyperlinked footer), and add an HTML body.

Touches local queue.json + pushes the rewritten entry to Apps Script via
the existing delete + re-add flow (cloud entries are dedup'd by `to`).
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import core  # noqa: E402

NEW_FOOTER = (
    "[Github](https://github.com/JacobisGreat) "
    "· [LinkedIn](https://www.linkedin.com/in/0xjacobj/)"
)

OLD_FOOTER_RE = re.compile(
    r"\(Github\)\s*https?://github\.com/JacobisGreat\s*\n"
    r"\(Linkedin\)\s*https?://www\.linkedin\.com/in/0xjacobj/?",
    re.IGNORECASE,
)


def rewrite_body(body: str) -> str:
    body = body.replace("I'm 17 and", "I'm 18 and")
    # If old footer present, replace; if footer is gone or different, append fresh one.
    if OLD_FOOTER_RE.search(body):
        body = OLD_FOOTER_RE.sub(NEW_FOOTER, body)
    elif "[Github](" not in body:
        # Append at the end (after a blank line) if no footer at all.
        body = body.rstrip() + "\n\n" + NEW_FOOTER
    return body


def main():
    core.load_env()
    queue = core.load_queue()

    # Step 1: pull cloud snapshot to learn which entries already sent.
    cloud = core.fetch_gas_queue()
    if not cloud.get("ok"):
        print(f"WARNING: cloud read failed: {cloud.get('error')}")
        cloud_status = {}
    else:
        cloud_status = {e["to"].lower(): e for e in cloud.get("queue", [])}
        print(f"cloud holds {len(cloud_status)} entries")

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
        print(f"synced {synced} stale-pending local entries -> sent (matches cloud)")

    # Step 3: rewrite still-pending entries to use new template + add html body.
    pending = [e for e in queue if e.get("status") == "pending"]
    print(f"local entries still pending: {len(pending)}")

    changed = 0
    for entry in pending:
        original_body = entry.get("body", "")
        new_body = rewrite_body(original_body)
        if new_body != original_body or "html" not in entry:
            entry["body"] = core.to_plain_body(new_body)
            entry["html"] = core.to_html_body(new_body)
            changed += 1

    core.save_queue(queue)
    try:
        core.encrypt_queue()
    except Exception:
        pass
    print(f"updated {changed} local entries (added html + new template)")

    # Step 4: push every still-pending entry to cloud (delete stale, then add).
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
