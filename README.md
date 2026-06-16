# ColdEmailMax

Paste an Apollo CSV export in, personalized cold emails go out the next morning
at 9:30 AM ET — automatically, even with your laptop closed.

## How it works

1. **`./run.sh "export.csv"`** (local) — for every contact, fetches the company's
   website, has Gemini write a one-line personalized hook, fills in the template
   from `email.txt`, queues everything in `queue.json` with tomorrow's date,
   encrypts it to `queue.enc` (AES-256), and pushes to GitHub.
2. **GitHub Actions** (cloud) — a cron fires at 9:00 AM America/New_York every
   day, decrypts the queue, sends all due emails through your Gmail (SMTP app
   password), spaces them out 20–45s apart, and commits the re-encrypted queue.

The repo is public (Actions is free there) but no contact data, email content,
or credentials are ever committed in plaintext — only `queue.enc`.

## One-time setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # then paste at least one LLM key into .env
```

### LLM providers (rate-limit resilience)

Line generation tries providers in order and **falls through to the next when
one rate-limits**, so adding a second provider multiplies your free headroom:

1. `GEMINI_API_KEY` — Gemini Flash, then Flash-Lite (same key, separate bucket)
2. `GROQ_API_KEY` — free + generous, https://console.groq.com/keys
3. `OPENROUTER_API_KEY` — free models, https://openrouter.ai/keys
4. `CEREBRAS_API_KEY` — free + fast, https://cloud.cerebras.ai

Set as many as you like. One company = one call (cached across its contacts).

GitHub repo secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `GMAIL_ADDRESS` | the Gmail you send from |
| `GMAIL_APP_PASSWORD` | from https://myaccount.google.com/apppasswords (needs 2FA on) |
| `QUEUE_KEY` | random hex key for queue encryption (same value as in local `.env`) |

## Daily use

```bash
./run.sh "apollo-contacts-export (1).csv"
```

That's it. Check `queue.json` afterwards if you're curious what's going out.

## Useful bits

- Manual/test send: Actions tab → "Send queued cold emails" → Run workflow
  (tick "force" to ignore the 9 AM guard).
- Already-queued addresses are skipped, so re-running on the same CSV is safe.
- Contacts at the same company share one researched line (one Gemini call per company).
