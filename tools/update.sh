#!/usr/bin/env bash
# Update Hushcut to the latest commit and (re)start the container.
#
#   ./tools/update.sh
#
# Rebuilds the image after pulling — Docker's layer cache makes this take only
# a few seconds when nothing but server code changed. Also safe to run just to
# make sure the container is up. Run it from cron for automatic updates, e.g.:
#   0 4 * * * cd /path/to/hushcut && ./tools/update.sh >> data/update.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

dc() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    else
        docker-compose "$@"
    fi
}

before=$(git rev-parse HEAD)
git pull --ff-only
after=$(git rev-parse HEAD)

if [ "$before" = "$after" ]; then
    echo "hushcut: already up to date ($(git rev-parse --short HEAD))"
else
    echo "hushcut: updated $(git rev-parse --short "$before") -> $(git rev-parse --short "$after")"
fi
dc up -d --build
