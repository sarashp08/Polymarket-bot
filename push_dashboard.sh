#!/usr/bin/env bash
# push_dashboard.sh — export bot state and push the dashboard to GitHub Pages.
#
# Usage (manual):
#   bash push_dashboard.sh
#
# Usage (cron, every 5 minutes):
#   */5 * * * * cd /home/user/Polymarket-bot && bash push_dashboard.sh >> push_dashboard.log 2>&1

set -euo pipefail

BRANCH="claude/polymarket-bot-01R8tiEBoUsv95u8RhHsVNmV"

cd "$(dirname "$0")"

# 1. Export state files → docs/data.json
python3 export_data.py

# 2. Stage the file
git add docs/data.json

# 3. Commit only if there are changes
if git diff --cached --quiet; then
    echo "No changes to push."
    exit 0
fi

git commit -m "chore: update dashboard data $(date -u '+%Y-%m-%d %H:%M UTC')"

# 4. Push with exponential-backoff retry
attempt=0
delay=2
while ! git push -u origin "$BRANCH"; do
    attempt=$((attempt + 1))
    if [ $attempt -ge 4 ]; then
        echo "Push failed after $attempt attempts." >&2
        exit 1
    fi
    echo "Push failed, retrying in ${delay}s…"
    sleep $delay
    delay=$((delay * 2))
done

echo "Dashboard updated successfully."
