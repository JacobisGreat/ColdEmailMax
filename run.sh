#!/bin/zsh
# ColdEmailMax — one command: research, queue, encrypt, push.
# Usage: ./run.sh "apollo-contacts-export (1).csv"
set -e
cd "$(dirname "$0")"

if [ -z "$1" ]; then
  echo "usage: ./run.sh \"apollo-export.csv\""
  exit 1
fi

.venv/bin/python enqueue.py "$1"

# Hand the queue to Google Apps Script — it sends at 9 AM ET from Google's cloud.
if grep -q '^GAS_WEB_APP_URL=' .env; then
  .venv/bin/python push_gas.py
fi

# Encrypt the queue — the repo is public, queue.json never leaves this machine.
export $(grep QUEUE_KEY .env)
openssl enc -aes-256-cbc -pbkdf2 -salt -in queue.json -out queue.enc -pass env:QUEUE_KEY

git add queue.enc
if git diff --cached --quiet; then
  echo "Nothing new to push."
else
  git commit -m "queue batch $(date +%F)"
  git push
  echo "Pushed — emails go out tomorrow at 9:00 AM ET."
fi
