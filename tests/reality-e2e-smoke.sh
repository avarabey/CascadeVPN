#!/usr/bin/env bash
# Root-only, read-only-to-production E2E check of the public VLESS Reality path.
# It derives one ephemeral client config from Xray's active server config,
# starts a second loopback-only Xray process, and curls through its SOCKS port.
set -euo pipefail

umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/reality_smoke_config.py"
XRAY_CONFIG="${REALITY_SMOKE_XRAY_CONFIG:-/usr/local/x-ui/bin/config.json}"
XRAY_BINARY="${REALITY_SMOKE_XRAY_BINARY:-/usr/local/x-ui/bin/xray-linux-amd64}"
PUBLIC_SERVER="${REALITY_SMOKE_SERVER:-ffknd.ru}"
PUBLIC_PORT="${REALITY_SMOKE_PORT:-443}"
INBOUND_TAG="${REALITY_SMOKE_INBOUND_TAG:-}"
CHECK_URL="https://www.cloudflare.com/cdn-cgi/trace"

TMP_ROOT="${TMPDIR:-/tmp}"
TMP_DIR=""
CLIENT_CONFIG=""
XRAY_LOG=""
CURL_BODY=""
CURL_ERROR=""
XRAY_PID=""

ok() {
  printf '\033[32m[OK ]\033[0m %s\n' "$*"
}

require_root_controlled() {
  local metadata owner mode
  metadata="$(stat -c '%u %a' -- "$1")" || bad "не удалось проверить владельца файла smoke-теста"
  read -r owner mode <<<"$metadata"
  [[ "$owner" == "0" ]] || bad "root-only smoke отказывается читать файл не от root"
  (( (8#$mode & 8#022) == 0 )) \
    || bad "root-only smoke отказывается читать group/world-writable файл"
}

bad() {
  printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2
  exit 1
}

cleanup() {
  local ignored_status=$?
  trap - EXIT HUP INT TERM
  if [[ "$XRAY_PID" =~ ^[0-9]+$ ]] && kill -0 "$XRAY_PID" 2>/dev/null; then
    kill -TERM "$XRAY_PID" 2>/dev/null || true
    for _ in {1..30}; do
      kill -0 "$XRAY_PID" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$XRAY_PID" 2>/dev/null; then
      kill -KILL "$XRAY_PID" 2>/dev/null || true
    fi
    wait "$XRAY_PID" 2>/dev/null || true
  fi
  if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
    local artifact
    for artifact in "$CLIENT_CONFIG" "$XRAY_LOG" "$CURL_BODY" "$CURL_ERROR"; do
      [[ -z "$artifact" ]] || rm -f -- "$artifact"
    done
    rmdir -- "$TMP_DIR" 2>/dev/null || true
  fi
  return "$ignored_status"
}

on_signal() {
  exit 130
}

trap cleanup EXIT
trap on_signal HUP INT TERM

[[ "$(id -u)" == "0" ]] || bad "запустите smoke от root: он читает активный Xray config"
ulimit -c 0
command -v python3 >/dev/null 2>&1 || bad "нужен Python 3"
command -v curl >/dev/null 2>&1 || bad "нужен curl"
command -v stat >/dev/null 2>&1 || bad "нужен GNU stat"
[[ -r "$HELPER" ]] || bad "не найден stdlib helper smoke-теста"
[[ -f "$XRAY_CONFIG" && ! -L "$XRAY_CONFIG" ]] || bad "active Xray config недоступен или является symlink"
[[ -x "$XRAY_BINARY" && ! -L "$XRAY_BINARY" ]] || bad "Xray binary недоступен или является symlink"
[[ "$PUBLIC_PORT" =~ ^[0-9]+$ ]] || bad "REALITY_SMOKE_PORT должен быть числом"
require_root_controlled "$SCRIPT_DIR"
require_root_controlled "$HELPER"
require_root_controlled "$XRAY_CONFIG"
require_root_controlled "$XRAY_BINARY"

TMP_DIR="$(mktemp -d "$TMP_ROOT/ffknd-reality-smoke.XXXXXXXX")"
chmod 0700 "$TMP_DIR"
CLIENT_CONFIG="$TMP_DIR/client.json"
XRAY_LOG="$TMP_DIR/xray.log"
CURL_BODY="$TMP_DIR/trace.txt"
CURL_ERROR="$TMP_DIR/curl.err"
touch "$XRAY_LOG" "$CURL_BODY" "$CURL_ERROR"
chmod 0600 "$XRAY_LOG" "$CURL_BODY" "$CURL_ERROR"

SOCKS_PORT="$(python3 "$HELPER" free-port)" \
  || bad "не удалось выбрать свободный loopback SOCKS-порт"
[[ "$SOCKS_PORT" =~ ^[0-9]+$ ]] || bad "helper вернул некорректный SOCKS-порт"

BUILD_ARGUMENTS=(
  build
  --source "$XRAY_CONFIG"
  --destination "$CLIENT_CONFIG"
  --server "$PUBLIC_SERVER"
  --server-port "$PUBLIC_PORT"
  --socks-port "$SOCKS_PORT"
)
if [[ -n "$INBOUND_TAG" ]]; then
  BUILD_ARGUMENTS+=(--inbound-tag "$INBOUND_TAG")
fi
python3 "$HELPER" "${BUILD_ARGUMENTS[@]}" \
  || bad "не удалось безопасно собрать ephemeral Xray client config"
[[ "$(stat -c '%a' "$CLIENT_CONFIG")" == "600" ]] \
  || bad "ephemeral Xray client config имеет небезопасные права"

"$XRAY_BINARY" run -c "$CLIENT_CONFIG" >"$XRAY_LOG" 2>&1 &
XRAY_PID=$!

if ! python3 "$HELPER" wait-port --port "$SOCKS_PORT" --timeout 10; then
  kill -0 "$XRAY_PID" 2>/dev/null \
    || bad "ephemeral Xray завершился до открытия SOCKS-порта"
  bad "ephemeral Xray не открыл loopback SOCKS-порт"
fi
kill -0 "$XRAY_PID" 2>/dev/null || bad "ephemeral Xray неожиданно завершился"

if ! curl --fail --silent --show-error --max-time 30 \
  --proto '=https' --tlsv1.2 --noproxy '' \
  --socks5-hostname "127.0.0.1:$SOCKS_PORT" \
  --output "$CURL_BODY" --stderr "$CURL_ERROR" \
  "$CHECK_URL"; then
  bad "HTTPS-запрос через VLESS Reality не прошёл"
fi
python3 "$HELPER" validate-trace --path "$CURL_BODY" \
  || bad "контрольный HTTPS endpoint вернул неожиданный ответ"

ok "реальный VLESS Reality client прошёл через public :$PUBLIC_PORT и Nginx passthrough"
