# ColdEmailMax

Paste an Apollo CSV export in, personalized cold emails go out the next morning
at 9:00 AM ET — automatically, even with your laptop closed.

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
cp .env.example .env   # then paste your GEMINI_API_KEY into .env
```

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
