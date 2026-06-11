#!/bin/zsh
# ColdEmailMax — local fallback sender (launchd runs this daily at 9:00 AM ET).
# Same logic as the GitHub Action: decrypt -> send due emails -> re-encrypt -> push.
set -e
cd "$(dirname "$0")"

set -a; source .env; set +a

git pull -q --rebase || true
openssl enc -d -aes-256-cbc -pbkdf2 -in queue.enc -out queue.json -pass env:QUEUE_KEY

.venv/bin/python send.py "$@"

openssl enc -aes-256-cbc -pbkdf2 -salt -in queue.json -out queue.enc -pass env:QUEUE_KEY
git add queue.enc
if ! git diff --cached --quiet; then
  git commit -q -m "queue state (local send) $(date +%FT%T)"
  git push -q || true
fi
