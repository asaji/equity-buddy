#!/bin/bash
set -e

PUID=${PUID:-99}
PGID=${PGID:-100}

groupmod -o -g "$PGID" nogroup 2>/dev/null || true
usermod -o -u "$PUID" nobody 2>/dev/null || true

chown -R nobody:nogroup /app/data /app/cookies 2>/dev/null || true

exec gosu nobody:nogroup "$@"
