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
- Vary your sentence structure. Do NOT always open with the company name or with "I".
- NEVER use em dashes or en dashes (— or –). Use commas, periods, or "and" instead.
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
def remove_dashes(text: str) -> str:
    """Strip em/en dashes (and '--') that out a message as AI-written.

    Spaced dashes acting as a clause break become a comma; tidy up the
    resulting punctuation so nothing reads awkwardly.
    """
    if not text:
        return text
    text = re.sub(r"\s*[\u2014\u2013\u2015\u2012\u2e3a\u2e3b]\s*", ", ", text)
    text = re.sub(r"\s+--\s+", ", ", text)
    text = re.sub(r",\s*,", ", ", text)          # collapse doubled commas
    text = re.sub(r"\s+,", ",", text)            # no space before comma
    text = re.sub(r",\s*([.!?;:])", r"\1", text)  # comma then end punctuation
    return text.strip()


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


class RateLimited(Exception):
    """A provider returned 429 / quota exhausted — try the next one."""


def _build_prompt(contact: dict, site_text: str) -> str:
    return PROMPT.format(
        first_name=contact["first_name"],
        title=contact["title"],
        company=contact["company"],
        industry=contact["industry"],
        keywords=(contact["keywords"] or "")[:1500],
        funding=contact["funding"] or "unknown",
        site_text=site_text or "(website unavailable — rely on keywords/industry)",
    )


def _call_gemini(model: str, prompt: str) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.85, "maxOutputTokens": 2000},
    }
    resp = requests.post(url, params={"key": key}, json=body, timeout=60)
    if resp.status_code == 429:
        raise RateLimited(f"gemini/{model} 429")
    if resp.status_code in (500, 503):
        raise RateLimited(f"gemini/{model} {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"gemini bad response: {json.dumps(data)[:200]}")


def _call_openai_compatible(base: str, key: str, model: str, prompt: str) -> str:
    """Works for Groq, OpenRouter, Cerebras, Together, etc."""
    resp = requests.post(
        base,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.85,
            "max_tokens": 200,
        },
        timeout=60,
    )
    if resp.status_code == 429:
        raise RateLimited(f"{base} 429")
    if resp.status_code in (500, 502, 503):
        raise RateLimited(f"{base} {resp.status_code}")
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def active_providers() -> list[dict]:
    """Build the fallback chain from whatever API keys are present in env."""
    chain: list[dict] = []
    if os.environ.get("GEMINI_API_KEY"):
        chain.append({"name": "gemini-2.5-flash",
                      "fn": lambda p: _call_gemini("gemini-2.5-flash", p)})
        chain.append({"name": "gemini-2.5-flash-lite",
                      "fn": lambda p: _call_gemini("gemini-2.5-flash-lite", p)})
    if os.environ.get("GROQ_API_KEY"):
        model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
        chain.append({"name": f"groq/{model}",
                      "fn": lambda p, m=model: _call_openai_compatible(
                          "https://api.groq.com/openai/v1/chat/completions",
                          os.environ["GROQ_API_KEY"], m, p)})
    if os.environ.get("OPENROUTER_API_KEY"):
        model = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
        chain.append({"name": f"openrouter/{model}",
                      "fn": lambda p, m=model: _call_openai_compatible(
                          "https://openrouter.ai/api/v1/chat/completions",
                          os.environ["OPENROUTER_API_KEY"], m, p)})
    if os.environ.get("CEREBRAS_API_KEY"):
        model = os.environ.get("CEREBRAS_MODEL", "llama-3.3-70b")
        chain.append({"name": f"cerebras/{model}",
                      "fn": lambda p, m=model: _call_openai_compatible(
                          "https://api.cerebras.ai/v1/chat/completions",
                          os.environ["CEREBRAS_API_KEY"], m, p)})
    return chain


def generate_line(contact: dict, site_text: str) -> tuple[str, str]:
    """Return (line, provider_name). Falls through providers on rate-limit."""
    prompt = _build_prompt(contact, site_text)
    chain = active_providers()
    if not chain:
        raise RuntimeError("No LLM provider configured (set GEMINI_API_KEY etc.)")

    last_err = None
    for provider in chain:
        try:
            text = provider["fn"](prompt)
            return remove_dashes(text.strip().strip('"').strip()), provider["name"]
        except RateLimited as e:
            last_err = e
            continue  # next provider
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All providers exhausted (last: {last_err})")


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
    return remove_dashes(subject), remove_dashes(body)


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
