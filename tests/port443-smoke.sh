#!/usr/bin/env bash
# Проверяет оба назначения одного публичного 443: обычный сайт и VPN CONNECT.
# Запускается после развёртывания с машины вне сервера.
set -euo pipefail

PUBLIC_URL="${PORTAL_PUBLIC_URL:-https://ffknd.ru}"
CLIENT_USER="${TT_CLIENT_USERNAME:-}"
CLIENT_PASSWORD="${TT_CLIENT_PASSWORD:-}"
WEB_ONLY="${PORT443_WEB_ONLY:-0}"

ok()  { printf '\033[32m[OK ]\033[0m %s\n' "$*"; }
bad() { printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || bad "нужен curl"

HOME_PAGE="$(curl --fail --silent --show-error --max-time 15 \
  --proto '=https' --tlsv1.2 "$PUBLIC_URL/")" \
  || bad "обычный HTTPS GET $PUBLIC_URL/ не прошёл"
[[ -n "$HOME_PAGE" ]] || bad "главная страница вернула пустой ответ"

if printf '%s' "$HOME_PAGE" | grep -Eiq 'trusttunnel|3x-ui|xray|vpn server'; then
  bad "публичная страница раскрывает внутренние названия сервисов"
fi
ok "обычный HTTPS GET обслуживается порталом"

HEALTH="$(curl --fail --silent --show-error --max-time 15 \
  --proto '=https' --tlsv1.2 "$PUBLIC_URL/api/health")" \
  || bad "health endpoint портала недоступен"
printf '%s' "$HEALTH" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' \
  || bad "health endpoint не подтвердил готовность"
ok "API портала готов"

if [[ "$WEB_ONLY" == "1" ]]; then
  ok "CONNECT-проверка пропущена (PORT443_WEB_ONLY=1)"
  exit 0
fi

[[ -n "$CLIENT_USER" && -n "$CLIENT_PASSWORD" ]] || bad \
  "для CONNECT задайте TT_CLIENT_USERNAME и TT_CLIENT_PASSWORD либо PORT443_WEB_ONLY=1"
[[ "$CLIENT_USER" != *$'\n'* && "$CLIENT_PASSWORD" != *$'\n'* ]] \
  || bad "учётные данные не должны содержать перевод строки"

escape_curl_config() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

SAFE_PUBLIC_URL="$(escape_curl_config "$PUBLIC_URL")"
SAFE_CLIENT_USER="$(escape_curl_config "$CLIENT_USER")"
SAFE_CLIENT_PASSWORD="$(escape_curl_config "$CLIENT_PASSWORD")"

# Секрет не передаётся аргументом curl и не попадает в список процессов.
CURL_CONFIG="$(mktemp "${TMPDIR:-/tmp}/ttx-port443.XXXXXX")"
trap 'rm -f "$CURL_CONFIG"' EXIT
chmod 0600 "$CURL_CONFIG"
{
  printf 'proxy = "%s"\n' "$SAFE_PUBLIC_URL"
  printf 'proxy-user = "%s:%s"\n' "$SAFE_CLIENT_USER" "$SAFE_CLIENT_PASSWORD"
  printf 'proxy-anyauth\n'
} >"$CURL_CONFIG"

TUNNEL_TRACE="$(curl --config "$CURL_CONFIG" --fail --silent --show-error \
  --max-time 25 https://www.cloudflare.com/cdn-cgi/trace)" \
  || bad "HTTPS CONNECT через тот же 443 не прошёл"
printf '%s\n' "$TUNNEL_TRACE" | grep -q '^ip=' \
  || bad "туннель вернул неожиданный ответ"
ok "VPN CONNECT проходит через тот же публичный 443"
