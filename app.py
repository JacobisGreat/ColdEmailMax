#!/usr/bin/env python3
"""ColdEmailMax — local web app.

Run:  ./serve.sh   (or: .venv/bin/python app.py)
Then open http://127.0.0.1:5000

Upload an Apollo or lead-list CSV, pick a send date, and the app researches
each company, writes a personalized line with Gemini, builds the emails, and
hands them to Google Apps Script to send at 9:30 AM ET — laptop can be off.
"""

import json
import os
import time
import uuid
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

import core

core.load_env()

# Auto-reschedule any past-due pending entries on startup.
try:
    _r = core.reschedule_past_due(push_to_cloud=True)
    if _r["rescheduled"]:
        print(f"[startup] rescheduled {_r['rescheduled']} past-due entries -> {_r['target_date']} "
              f"(cloud: {_r['cloud_ok']} ok / {_r['cloud_failed']} failed)")
except Exception as _e:
    print(f"[startup] reschedule skipped: {_e}")

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
        if not core.active_providers():
            yield sse({"type": "error", "message": "No LLM provider configured in .env"})
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
        cloud_ok = 0
        cloud_failed = 0
        gas_configured = bool(
            os.environ.get("GAS_WEB_APP_URL") and os.environ.get("GAS_TOKEN")
        )

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
                    line, provider = core.generate_line(c, site)
                    company_lines[c["company"]] = line
                    fresh = True
                else:
                    provider = "cached"
                    fresh = False
                line = company_lines[c["company"]]
                subject, raw_body = core.build_email(template, c, line)
                entry = {
                    "to": c["email"],
                    "first_name": c["first_name"],
                    "company": c["company"],
                    "subject": subject,
                    "body": core.to_plain_body(raw_body),
                    "html": core.to_html_body(raw_body),
                    "send_date": send_date,
                    "status": "pending",
                    "queued_at": datetime.now(core.ET).isoformat(),
                }
                queue.append(entry)
                already.add(c["email"])
                added += 1
                core.save_queue(queue)

                # Push this single entry to Apps Script RIGHT NOW so a crash
                # mid-batch never loses progress.
                cloud_status = "skip"
                cloud_error = None
                if gas_configured:
                    try:
                        result = core.push_to_gas([entry])
                        if result.get("ok"):
                            cloud_status = "ok"
                            cloud_ok += 1
                        else:
                            cloud_status = "fail"
                            cloud_error = result.get("error", "unknown")
                            cloud_failed += 1
                    except Exception as e:
                        cloud_status = "fail"
                        cloud_error = str(e)
                        cloud_failed += 1

                yield sse({**base, "status": "queued", "line": line,
                           "provider": provider, "cloud": cloud_status,
                           "cloud_error": cloud_error})
                if fresh:
                    time.sleep(2)  # gentle pacing across providers
            except Exception as e:
                yield sse({**base, "status": "error", "line": str(e)})

        try:
            core.encrypt_queue()
        except Exception:
            pass

        yield sse(
            {
                "type": "done",
                "added": added,
                "send_date": send_date,
                "cloud_ok": cloud_ok,
                "cloud_failed": cloud_failed,
                "gas_configured": gas_configured,
            }
        )
        JOBS.pop(job_id, None)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/queue")
def api_queue():
    """Combined view: local queue.json + cloud (Apps Script) snapshot."""
    local = core.load_queue()
    cloud = core.fetch_gas_queue()
    cloud_emails = set()
    if cloud.get("ok"):
        cloud_emails = {e.get("to", "").lower() for e in cloud.get("queue", [])}

    rows = []
    for e in local:
        rows.append({
            "to": e.get("to"),
            "first_name": e.get("first_name"),
            "company": e.get("company"),
            "subject": e.get("subject"),
            "body": e.get("body"),
            "send_date": e.get("send_date"),
            "status": e.get("status"),
            "queued_at": e.get("queued_at"),
            "sent_at": e.get("sent_at"),
            "error": e.get("error"),
            "in_cloud": e.get("to", "").lower() in cloud_emails,
        })
    rows.sort(key=lambda r: (r["status"] != "pending", r["send_date"] or "", r["queued_at"] or ""))

    summary = {
        "total": len(local),
        "pending": sum(1 for r in rows if r["status"] == "pending"),
        "sent": sum(1 for r in rows if r["status"] == "sent"),
        "errors": sum(1 for r in rows if r["status"] == "error"),
        "in_cloud": sum(1 for r in rows if r["in_cloud"]),
        "cloud_total": cloud.get("total") if cloud.get("ok") else None,
        "cloud_pending": cloud.get("pending") if cloud.get("ok") else None,
        "cloud_error": cloud.get("error") if not cloud.get("ok") else None,
        "next_fire": cloud.get("next_fire") if cloud.get("ok") else None,
    }
    return jsonify({"ok": True, "summary": summary, "rows": rows})


@app.route("/api/queue/reschedule", methods=["POST"])
def api_queue_reschedule():
    result = core.reschedule_past_due(push_to_cloud=True)
    return jsonify({"ok": True, **result})


@app.route("/api/queue/delete", methods=["POST"])
def api_queue_delete():
    data = request.get_json(silent=True) or {}
    email = (data.get("to") or "").lower()
    if not email:
        return jsonify({"ok": False, "error": "missing 'to'"}), 400

    queue = core.load_queue()
    before = len(queue)
    queue = [e for e in queue if not (e.get("to", "").lower() == email and e.get("status") == "pending")]
    removed_local = before - len(queue)
    core.save_queue(queue)
    try:
        core.encrypt_queue()
    except Exception:
        pass

    cloud_result = core.delete_from_gas(email)
    return jsonify({
        "ok": True,
        "removed_local": removed_local,
        "cloud": cloud_result,
    })


def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
