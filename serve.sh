#!/bin/zsh
# ColdEmailMax — launch the local web UI.
cd "$(dirname "$0")"
echo "ColdEmailMax UI -> http://127.0.0.1:5000  (Ctrl+C to stop)"
.venv/bin/python app.py
