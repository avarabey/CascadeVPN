#!/usr/bin/env bash
set -Eeuo pipefail

# One-shot production cutover for the audited ffknd.ru host topology.
# Run only after the portal TLS endpoint on 127.0.0.1:9443 is healthy.

umask 077

TOOLS_DIR="/root/ffknd-cutover-tools"
XUI_HELPER="${TOOLS_DIR}/xui_portal_cutover.py"
MODULE_SOURCE="${TOOLS_DIR}/70-ffknd-stream.conf"
ROUTER_SOURCE="${TOOLS_DIR}/ffknd-router.conf"
MODULE_DEST="/etc/nginx/modules-enabled/70-ffknd-stream.conf"
ROUTER_DEST="/etc/nginx/stream-conf.d/ffknd-router.conf"
STATE_DIR="/var/lib/ffknd-xui-cutover"

APPLY_STARTED=false
FINISHED=false

log() {
  printf '[ffknd-cutover] %s\n' "$*"
}

listen_line() {
  local port="$1"
  ss -H -ltnp "sport = :${port}" 2>/dev/null || true
}

wait_for_xray_loopback() {
  local attempt line
  for attempt in {1..60}; do
    line="$(listen_line 10443)"
    if [[ "$line" == *"127.0.0.1:10443"* && "$line" == *"xray"* ]] \
      && [[ -z "$(listen_line 443)" ]]; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

wait_for_nginx_public() {
  local attempt line
  for attempt in {1..60}; do
    line="$(listen_line 443)"
    if [[ "$line" == *":443"* && "$line" == *"nginx"* ]]; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

rollback() {
  local original_exit="$?"
  trap - ERR INT TERM
  [[ "$FINISHED" == true ]] && exit "$original_exit"

  log "ошибка; выполняю аварийный откат"
  if [[ -e "$MODULE_DEST" ]]; then
    install -d -m 0700 "$STATE_DIR"
    mv "$MODULE_DEST" "${STATE_DIR}/70-ffknd-stream.conf.disabled"
  fi
  if [[ -e "$ROUTER_DEST" ]]; then
    install -d -m 0700 "$STATE_DIR"
    mv "$ROUTER_DEST" "${STATE_DIR}/ffknd-router.conf.disabled"
  fi

  # A graceful quit could keep long-lived stream workers (and :443) alive.
  # TERM is intentional here: rollback must release the port immediately.
  systemctl kill --kill-who=all --signal=TERM nginx >/dev/null 2>&1 || true
  systemctl stop nginx >/dev/null 2>&1 || true

  if [[ "$APPLY_STARTED" == true && -f "${STATE_DIR}/state.json" ]]; then
    python3 "$XUI_HELPER" rollback || log "автоматический rollback 3x-ui не завершён"
  fi

  if nginx -t; then
    systemctl start nginx || true
  fi
  log "откат завершён; проверьте владельца :443 и Reality"
  exit "$original_exit"
}

trap rollback ERR INT TERM

if [[ "${EUID}" -ne 0 ]]; then
  log "запускать только от root"
  exit 1
fi

for required in "$XUI_HELPER" "$MODULE_SOURCE" "$ROUTER_SOURCE"; do
  if [[ ! -f "$required" || -L "$required" ]]; then
    log "нет обязательного обычного файла: $required"
    exit 1
  fi
done

if [[ -e "$MODULE_DEST" || -e "$ROUTER_DEST" ]]; then
  log "production stream-конфигурация уже существует; отказ от перезаписи"
  exit 1
fi

systemctl is-active --quiet x-ui
systemctl is-active --quiet trusttunnel
systemctl is-active --quiet ffknd-portal
systemctl is-active --quiet nginx
nginx -t
curl --fail --silent --show-error \
  --noproxy '*' \
  --resolve ffknd.ru:9443:127.0.0.1 \
  https://ffknd.ru:9443/api/health >/dev/null
python3 "$XUI_HELPER" dry-run

log "переношу локальный Reality inbound на 127.0.0.1:10443"
APPLY_STARTED=true
python3 "$XUI_HELPER" apply
wait_for_xray_loopback

log "включаю Nginx SNI router на :443"
install -d -m 0755 /etc/nginx/stream-conf.d
install -m 0644 "$ROUTER_SOURCE" "$ROUTER_DEST"
install -m 0644 "$MODULE_SOURCE" "$MODULE_DEST"
nginx -t
systemctl reload nginx
wait_for_nginx_public

curl --fail --silent --show-error \
  --noproxy '*' \
  --resolve ffknd.ru:443:127.0.0.1 \
  https://ffknd.ru/api/health >/dev/null
openssl s_client -connect 127.0.0.1:443 -servername cloud.ru \
  -verify_hostname cloud.ru -brief </dev/null >/dev/null

systemctl is-active --quiet x-ui
systemctl is-active --quiet trusttunnel
systemctl is-active --quiet ffknd-portal
systemctl is-active --quiet nginx

FINISHED=true
trap - ERR INT TERM
log "переключение завершено успешно"
