#!/usr/bin/env python3
"""ColdEmailMax — shared core logic.

Used by both the CLI (enqueue.py) and the web app (app.py):
CSV parsing/normalization, company research, Gemini line generation,
email assembly, queue persistence, encryption, and the Apps Script push.
"""

import csv
import html
import io
import json
import os
import re
import subprocess
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
- Keywords: {keywords}
- Funding/size: {funding}
- Website text (may be messy/partial):
---
{site_text}
---

Write exactly ONE sentence (max 28 words) that shows Jacob genuinely understands what \
{company} does and why it excites him specifically. Requirements:
- Reference something concrete and real about the company (their actual product, mission, or a real detail) — never generic flattery.
- First person, casual but sharp, like a smart teenager who did his homework. No corporate buzzwords.
- Vary your sentence structure — do NOT always open with the company name or with "I".
- Do NOT invent facts. Only use what's in the info above.
- Do NOT start with "I'm" (the previous paragraph already does).
- Output ONLY the sentence. No quotes, no preamble.
"""


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
def load_env():
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


# --------------------------------------------------------------------------- #
# CSV parsing — supports the Apollo export AND the simpler lead-list format
# --------------------------------------------------------------------------- #
def _first(row: dict, *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return ""


def normalize_row(row: dict) -> dict:
    """Map either CSV schema onto one common contact dict."""
    funding = ""
    if _first(row, "Latest Funding"):
        funding = _first(row, "Latest Funding")
        amt = _first(row, "Latest Funding Amount")
        if amt:
            funding += f" (${amt})"
    elif _first(row, "CompanyRevenueRange"):
        funding = "revenue " + _first(row, "CompanyRevenueRange")

    keywords = _first(row, "Keywords", "CompanyKeywords").replace(";", ", ")

    return {
        "first_name": _first(row, "First Name", "FirstName"),
        "last_name": _first(row, "Last Name", "LastName"),
        "title": _first(row, "Title", "JobTitle"),
        "company": _first(
            row, "Company Name for Emails", "Company Name", "CompanyName"
        ),
        "email": _first(row, "Email").lower(),
        "industry": _first(row, "Industry", "CompanyIndustry"),
        "keywords": keywords,
        "website": _first(row, "Website", "CompanyDomain"),
        "funding": funding,
    }


def parse_csv(text: str) -> list[dict]:
    """Parse CSV text into normalized contact dicts that have an email."""
    reader = csv.DictReader(io.StringIO(text))
    contacts = []
    for raw in reader:
        norm = normalize_row(raw)
        if norm["email"]:
            contacts.append(norm)
    return contacts


def detect_format(text: str) -> str:
    header = text.splitlines()[0] if text else ""
    if "Apollo Contact Id" in header or "Company Name for Emails" in header:
        return "Apollo export"
    if "CompanyDomain" in header or "JobTitle" in header:
        return "Lead list"
    return "Generic CSV"


# --------------------------------------------------------------------------- #
# Research + generation
# --------------------------------------------------------------------------- #
def fetch_site_text(url: str) -> str:
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
    except Exception:
        return ""
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(re.sub(r"\s+", " ", raw)).strip()
    return text[:6000]


def gemini_line(api_key: str, contact: dict, site_text: str) -> str:
    prompt = PROMPT.format(
        first_name=contact["first_name"],
        title=contact["title"],
        company=contact["company"],
        industry=contact["industry"],
        keywords=(contact["keywords"] or "")[:1500],
        funding=contact["funding"] or "unknown",
        site_text=site_text or "(website unavailable — rely on keywords/industry)",
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 2000},
    }
    for attempt in range(6):
        resp = requests.post(
            GEMINI_URL, params={"key": api_key}, json=body, timeout=60
        )
        if resp.status_code in (429, 500, 503):
            time.sleep(15 * (attempt + 1))
            continue
        resp.raise_for_status()
        data = resp.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise RuntimeError(f"Unexpected Gemini response: {json.dumps(data)[:300]}")
        return text.strip().strip('"').strip()
    raise RuntimeError("Gemini retries exhausted (rate limited)")


def build_email(template: str, contact: dict, company_line: str) -> tuple[str, str]:
    subject = ""
    body_lines = []
    for line in template.strip("\n").splitlines():
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip("\n")
    body = body.replace("Hi Name,", f"Hi {contact['first_name']},")
    body = re.sub(r"(?m)^1 line about the compan.*$", company_line, body)
    body = body.replace("[company]", contact["company"])
    return subject, body


# --------------------------------------------------------------------------- #
# Queue persistence + delivery
# --------------------------------------------------------------------------- #
def load_queue() -> list[dict]:
    if QUEUE_FILE.exists():
        return json.loads(QUEUE_FILE.read_text())
    return []


def save_queue(queue: list[dict]):
    QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def next_weekday(days_ahead: int = 1) -> str:
    """Next business day (Mon–Fri) at least `days_ahead` days out, YYYY-MM-DD ET."""
    d = datetime.now(ET) + timedelta(days=days_ahead)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def encrypt_queue():
    key = os.environ.get("QUEUE_KEY")
    if not key:
        return
    env = {**os.environ, "QUEUE_KEY": key}
    subprocess.run(
        [
            "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
            "-in", str(QUEUE_FILE), "-out", str(ROOT / "queue.enc"),
            "-pass", "env:QUEUE_KEY",
        ],
        check=True,
        env=env,
    )


def push_to_gas(entries: list[dict]) -> dict:
    """Send pending entries to the Apps Script web app. Returns its JSON reply."""
    url = os.environ.get("GAS_WEB_APP_URL")
    token = os.environ.get("GAS_TOKEN")
    if not url or not token:
        return {"ok": False, "error": "GAS_WEB_APP_URL/GAS_TOKEN not configured"}
    resp = requests.post(
        url,
        json={"token": token, "entries": entries},
        timeout=90,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp.json()
