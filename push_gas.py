#!/usr/bin/env python3
"""Push pending queue.json entries to the Google Apps Script web app."""

import sys

import core


def main():
    core.load_env()
    queue = core.load_queue()
    pending = [e for e in queue if e["status"] == "pending"]
    if not pending:
        print("No pending entries to push.")
        return
    data = core.push_to_gas(pending)
    if not data.get("ok"):
        sys.exit(f"Apps Script rejected the push: {data}")
    print(f"Pushed {data['added']} new entries (Apps Script now holds {data['total']}).")
    print("They'll go out at 9:00 AM ET on their send date — laptop can be off.")


if __name__ == "__main__":
    main()
