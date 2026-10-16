#!/usr/bin/env bash
# Wait for the local portal to become ready after a controlled restart.
set -euo pipefail

URL="${PORTAL_HEALTH_URL:-http://127.0.0.1:8080/api/health}"
ATTEMPTS="${PORTAL_HEALTH_ATTEMPTS:-30}"

[[ "$ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || {
  printf '%s\n' '[FAIL] PORTAL_HEALTH_ATTEMPTS must be a positive integer' >&2
  exit 2
}

for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
  if curl --fail --silent --show-error --max-time 2 \
      --output /dev/null "$URL" 2>/dev/null; then
    printf '[OK ] portal health ready (%s)\n' "$URL"
    exit 0
  fi
  sleep 0.5
done

printf '[FAIL] portal health did not become ready (%s)\n' "$URL" >&2
exit 1
