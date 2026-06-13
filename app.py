#!/usr/bin/env python3
"""ColdEmailMax — local web app.

Run:  ./serve.sh   (or: .venv/bin/python app.py)
Then open http://127.0.0.1:5000

Upload an Apollo or lead-list CSV, pick a send date, and the app researches
each company, writes a personalized line with Gemini, builds the emails, and
hands them to Google Apps Script to send at 9:00 AM ET — laptop can be off.
"""

import json
import os
import time
import uuid
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

import core

core.load_env()

app = Flask(__name__)
JOBS: dict[str, dict] = {}  # job_id -> {"contacts": [...], "format": str}


@app.route("/")
def index():
    configured = bool(os.environ.get("GAS_WEB_APP_URL") and os.environ.get("GAS_TOKEN"))
    return render_template(
        "index.html",
        default_date=core.next_weekday(1),
        gas_configured=configured,
    )


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if not file:
        return jsonify({"ok": False, "error": "no file"}), 400
    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"ok": False, "error": "could not read file as text/CSV"}), 400

    contacts = core.parse_csv(text)
    if not contacts:
        return jsonify({"ok": False, "error": "no rows with an email found"}), 400

    fmt = core.detect_format(text)
    existing = {e["to"].lower() for e in core.load_queue()}
    companies = sorted({c["company"] for c in contacts if c["company"]})

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"contacts": contacts, "format": fmt}

    preview = [
        {
            "name": f"{c['first_name']} {c['last_name']}".strip(),
            "title": c["title"],
            "company": c["company"],
            "email": c["email"],
            "duplicate": c["email"] in existing,
        }
        for c in contacts
    ]
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "format": fmt,
            "count": len(contacts),
            "companies": len(companies),
            "new": sum(1 for p in preview if not p["duplicate"]),
            "contacts": preview,
        }
    )


@app.route("/stream")
def stream():
    job_id = request.args.get("job_id", "")
    send_date = request.args.get("send_date", "")
    job = JOBS.get(job_id)

    def sse(event: dict) -> str:
        return f"data: {json.dumps(event)}\n\n"

    def generate():
        if not job:
            yield sse({"type": "error", "message": "session expired — re-upload"})
            return
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            yield sse({"type": "error", "message": "GEMINI_API_KEY missing in .env"})
            return
        if not send_date or not _valid_date(send_date):
            yield sse({"type": "error", "message": "invalid send date"})
            return

        template = core.TEMPLATE_FILE.read_text()
        queue = core.load_queue()
        already = {e["to"].lower() for e in queue}
        contacts = job["contacts"]

        yield sse({"type": "start", "total": len(contacts), "send_date": send_date})

        company_lines: dict[str, str] = {}
        site_cache: dict[str, str] = {}
        added = 0
        new_entries = []

        for i, c in enumerate(contacts):
            base = {"type": "contact", "i": i, "name": f"{c['first_name']} {c['last_name']}".strip(),
                    "company": c["company"], "email": c["email"]}
            if c["email"] in already:
                yield sse({**base, "status": "skipped", "line": "already queued"})
                continue

            try:
                if c["company"] not in company_lines:
                    site = site_cache.get(c["website"])
                    if site is None:
                        site = core.fetch_site_text(c["website"])
                        site_cache[c["website"]] = site
                    company_lines[c["company"]] = core.gemini_line(api_key, c, site)
                    fresh = True
                else:
                    fresh = False
                line = company_lines[c["company"]]
                subject, body = core.build_email(template, c, line)
                entry = {
                    "to": c["email"],
                    "first_name": c["first_name"],
                    "company": c["company"],
                    "subject": subject,
                    "body": body,
                    "send_date": send_date,
                    "status": "pending",
                    "queued_at": datetime.now(core.ET).isoformat(),
                }
                queue.append(entry)
                new_entries.append(entry)
                already.add(c["email"])
                added += 1
                core.save_queue(queue)
                yield sse({**base, "status": "queued", "line": line})
                if fresh:
                    time.sleep(5)  # respect Gemini free-tier rate limit
            except Exception as e:  # keep going; report the failure inline
                yield sse({**base, "status": "error", "line": str(e)})

        # Deliver: encrypt locally + hand pending entries to Apps Script.
        gas_result = {"ok": False, "error": "not configured"}
        try:
            core.encrypt_queue()
        except Exception:
            pass
        if new_entries:
            try:
                gas_result = core.push_to_gas(new_entries)
            except Exception as e:
                gas_result = {"ok": False, "error": str(e)}

        yield sse(
            {
                "type": "done",
                "added": added,
                "send_date": send_date,
                "gas": gas_result,
            }
        )
        JOBS.pop(job_id, None)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
