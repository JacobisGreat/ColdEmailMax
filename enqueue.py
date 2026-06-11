#!/usr/bin/env python3
"""ColdEmailMax — enqueue step.

Usage:
    python enqueue.py "apollo-contacts-export (1).csv"

Reads an Apollo contacts export, researches each company (website + Gemini),
fills in the email template, and queues every email in queue.json with a
send date of tomorrow (9:00 AM America/New_York, fired by GitHub Actions).
"""

import csv
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).parent
QUEUE_FILE = ROOT / "queue.json"
TEMPLATE_FILE = ROOT / "email.txt"
ET = ZoneInfo("America/New_York")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

PROMPT = """You are helping write ONE sentence for a cold email from Jacob, a 17-year-old \
developer who has shipped real products (ShieldClaw, an AI-agent security layer that won \
HackCanada, and NoDox, an OSINT self-audit tool). He is emailing {first_name} ({title}) at \
{company} asking about a summer engineering role.

Here is what we know about {company}:
- Industry: {industry}
- Apollo keywords: {keywords}
- Funding: {funding}
- Website text (may be messy/partial):
---
{site_text}
---

Write exactly ONE sentence (max 28 words) that shows Jacob genuinely understands what \
{company} does and why it excites him specifically. Requirements:
- Reference something concrete and real about the company (their actual product, mission, or a real detail) — never generic flattery.
- First person, casual but sharp, like a smart teenager who did his homework. No corporate buzzwords.
- Do NOT invent facts. Only use what's in the info above.
- Do NOT start with "I'm" (the previous paragraph already does).
- Output ONLY the sentence. No quotes, no preamble.
"""


def load_env():
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def fetch_site_text(url: str) -> str:
    """Fetch a company homepage and reduce it to readable text."""
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        raw = resp.text
    except Exception as e:
        print(f"    ! could not fetch {url}: {e}")
        return ""
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(re.sub(r"\s+", " ", raw)).strip()
    return text[:6000]


def gemini_line(api_key: str, row: dict, site_text: str) -> str:
    funding = ""
    if row.get("Latest Funding"):
        funding = f"{row['Latest Funding']}"
        if row.get("Latest Funding Amount"):
            funding += f" (${row['Latest Funding Amount']})"
    prompt = PROMPT.format(
        first_name=row.get("First Name", ""),
        title=row.get("Title", ""),
        company=row.get("Company Name for Emails") or row.get("Company Name", ""),
        industry=row.get("Industry", ""),
        keywords=(row.get("Keywords", "") or "")[:1500],
        funding=funding or "unknown",
        site_text=site_text or "(website unavailable — rely on keywords/industry)",
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 2000},
    }
    for attempt in range(6):
        resp = requests.post(
            GEMINI_URL,
            params={"key": api_key},
            json=body,
            timeout=60,
        )
        if resp.status_code in (429, 500, 503):
            wait = 15 * (attempt + 1)
            print(f"    ~ Gemini {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            raise RuntimeError(f"Unexpected Gemini response: {json.dumps(data)[:500]}")
        return text.strip().strip('"').strip()
    raise RuntimeError("Gemini rate limit retries exhausted")


def build_email(template: str, row: dict, company_line: str) -> tuple[str, str]:
    company = row.get("Company Name for Emails") or row.get("Company Name", "")
    lines = template.strip("\n").splitlines()
    subject = ""
    body_lines = []
    for line in lines:
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip("\n")
    body = body.replace("Hi Name,", f"Hi {row.get('First Name', '').strip()},")
    body = re.sub(r"(?m)^1 line about the compan.*$", company_line, body)
    body = body.replace("[company]", company)
    return subject, body


def main():
    if len(sys.argv) < 2:
        sys.exit('usage: python enqueue.py "apollo-export.csv"')
    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        sys.exit(f"file not found: {csv_path}")

    load_env()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY missing — put it in .env")

    template = TEMPLATE_FILE.read_text()

    queue = []
    if QUEUE_FILE.exists():
        queue = json.loads(QUEUE_FILE.read_text())
    already_queued = {e["to"].lower() for e in queue}

    send_date = (datetime.now(ET) + timedelta(days=1)).strftime("%Y-%m-%d")

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Email")]

    print(f"{len(rows)} contacts in CSV — queueing for {send_date} 9:00 AM ET\n")

    company_lines: dict[str, str] = {}
    site_cache: dict[str, str] = {}
    added = 0

    for row in rows:
        email = row["Email"].strip().lower()
        first = row.get("First Name", "").strip()
        company = (row.get("Company Name for Emails") or row.get("Company Name", "")).strip()
        if email in already_queued:
            print(f"  - {email} already queued, skipping")
            continue

        print(f"  * {first} @ {company} <{email}>")
        if company not in company_lines:
            website = row.get("Website", "").strip()
            if website not in site_cache:
                site_cache[website] = fetch_site_text(website)
            line = gemini_line(api_key, row, site_cache[website])
            company_lines[company] = line
            print(f"      line: {line}")
            time.sleep(5)  # stay friendly with the free-tier rate limit

        subject, body = build_email(template, row, company_lines[company])
        queue.append(
            {
                "to": email,
                "first_name": first,
                "company": company,
                "subject": subject,
                "body": body,
                "send_date": send_date,
                "status": "pending",
                "queued_at": datetime.now(ET).isoformat(),
            }
        )
        already_queued.add(email)
        added += 1
        QUEUE_FILE.write_text(json.dumps(queue, indent=2))  # save progress as we go

    QUEUE_FILE.write_text(json.dumps(queue, indent=2))
    print(f"\nQueued {added} emails -> {QUEUE_FILE.name}")
    print("Run `git add queue.json && git commit -m 'queue batch' && git push` "
          "or use run.sh to do it automatically.")


if __name__ == "__main__":
    main()
