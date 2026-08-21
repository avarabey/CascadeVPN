#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

for file in install/ttx-install.sh deploy/deploy.sh deploy/bootstrap-ubuntu.sh \
            deploy/harden-memory.sh \
            tests/e2e-smoke.sh tests/port443-smoke.sh tests/reality-e2e-smoke.sh \
            tests/check.sh \
            tests/verify-trusttunnel-routing-source.sh; do
  bash -n "$file"
done

python3 -m json.tool bridge/bridge.example.json >/dev/null
python3 -m json.tool compose/bridge.docker.json >/dev/null
python3 -m json.tool templates/compat.json >/dev/null
python3 -m json.tool tests/fixtures/trusttunnel-v1.0.33-routing-contract.json >/dev/null

PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/ttx-pycache" python3 -m py_compile bridge/ttx_bridge.py
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/ttx-pycache" python3 -m py_compile \
  tests/reality_smoke_config.py
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/ttx-pycache" python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/ttx-pycache" python3 -m py_compile \
  portal/app/*.py portal/app/services/*.py
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/ttx-pycache" python3 -m unittest discover \
  -s portal/tests -p 'test_*.py'

if grep -ERiq 'trusttunnel|3x-ui|xray|vpn server' portal/app/static; then
  printf '%s\n' 'публичная статика раскрывает внутренние названия сервисов' >&2
  exit 1
fi

if command -v node >/dev/null 2>&1; then
  for file in portal/app/static/js/*.js; do
    node --check "$file"
  done
else
  printf '%s\n' 'node не найден: синтаксическая проверка frontend JS пропущена'
fi

if command -v docker >/dev/null 2>&1; then
  docker compose -f compose/docker-compose.yml config >/dev/null
else
  printf '%s\n' 'docker не найден: проверка compose пропущена'
fi

printf '%s\n' 'Все доступные локальные проверки пройдены.'
