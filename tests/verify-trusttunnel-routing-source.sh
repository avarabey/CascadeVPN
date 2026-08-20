#!/usr/bin/env bash
# Opt-in provenance check. tests/check.sh validates only this file's syntax;
# it does not execute the network-dependent check during the regular suite.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTRACT="$ROOT/tests/fixtures/trusttunnel-v1.0.33-routing-contract.json"

SOURCE_URL="$(python3 -c \
  'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["upstream"]["source_url"])' \
  "$CONTRACT")"
EXPECTED_SHA256="$(python3 -c \
  'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["upstream"]["source_sha256"])' \
  "$CONTRACT")"

SOURCE_FILE="$(mktemp "${TMPDIR:-/tmp}/trusttunnel-routing-source.XXXXXX")"
trap 'rm -f "$SOURCE_FILE"' EXIT

curl --fail --silent --show-error --location "$SOURCE_URL" --output "$SOURCE_FILE"

if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL_SHA256="$(sha256sum "$SOURCE_FILE" | awk '{print $1}')"
else
  ACTUAL_SHA256="$(shasum -a 256 "$SOURCE_FILE" | awk '{print $1}')"
fi

if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  printf 'TrustTunnel routing source SHA-256 mismatch: expected %s, got %s\n' \
    "$EXPECTED_SHA256" "$ACTUAL_SHA256" >&2
  exit 1
fi

printf 'TrustTunnel routing source matches pinned SHA-256 %s\n' "$EXPECTED_SHA256"
