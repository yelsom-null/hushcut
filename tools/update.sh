#!/usr/bin/env bash
# Update Hushcut to the latest commit and apply it to the running container.
#
#   ./tools/update.sh
#
# Server code is bind-mounted into the container, so most updates only need a
# restart; the image is rebuilt only when Dockerfile/docker-compose.yml change.
# Run it from cron for automatic updates, e.g. daily at 4am:
#   0 4 * * * cd /path/to/hushcut && ./tools/update.sh >> data/update.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

before=$(git rev-parse HEAD)
git pull --ff-only
after=$(git rev-parse HEAD)

if [ "$before" = "$after" ]; then
    echo "hushcut: already up to date ($(git rev-parse --short HEAD))"
    exit 0
fi

echo "hushcut: updated $(git rev-parse --short "$before") -> $(git rev-parse --short "$after")"
if git diff --name-only "$before" "$after" | grep -qE '^(Dockerfile|docker-compose\.yml)$'; then
    echo "hushcut: container definition changed - rebuilding"
    docker compose up -d --build
else
    echo "hushcut: restarting with updated code"
    docker compose restart hushcut
fi
