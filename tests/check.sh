#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

for file in install/ttx-install.sh deploy/deploy.sh deploy/bootstrap-ubuntu.sh tests/e2e-smoke.sh tests/check.sh; do
  bash -n "$file"
done

python3 -m json.tool bridge/bridge.example.json >/dev/null
python3 -m json.tool compose/bridge.docker.json >/dev/null
python3 -m json.tool templates/compat.json >/dev/null

PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/ttx-pycache" python3 -m py_compile bridge/ttx_bridge.py
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/ttx-pycache" python3 -m unittest discover -s tests -p 'test_*.py'

if command -v docker >/dev/null 2>&1; then
  docker compose -f compose/docker-compose.yml config >/dev/null
else
  printf '%s\n' 'docker не найден: проверка compose пропущена'
fi

printf '%s\n' 'Все доступные локальные проверки пройдены.'
