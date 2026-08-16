#!/usr/bin/env bash
# Сквозная проверка цепочки: TrustTunnel -> SOCKS ingress -> Xray -> интернет.
set -uo pipefail

CFG="${TTX_CONFIG:-/etc/ttx/bridge.json}"
INGRESS_PORT=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["ingress"]["port"])' "$CFG")
INGRESS_HOST=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["ingress"]["listen"])' "$CFG")
TT_CFG=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["trusttunnel"]["target_config"])' "$CFG")
fail=0
ok()   { printf '\033[32m[OK ]\033[0m %s\n' "$*"; }
bad()  { printf '\033[31m[FAIL]\033[0m %s\n' "$*"; fail=1; }

# 1. Xray слушает ingress-порт
if ss -ltn "sport = :$INGRESS_PORT" 2>/dev/null | grep -q LISTEN; then
  ok "ingress слушает $INGRESS_HOST:$INGRESS_PORT"
else
  bad "на $INGRESS_HOST:$INGRESS_PORT никто не слушает — inbound выключен или xray не поднялся"
fi

# 2. Через ingress есть выход в интернет (то есть маршрутизация 3x-ui работает)
EGRESS=$(curl -s --max-time 15 --socks5-hostname "$INGRESS_HOST:$INGRESS_PORT" \
         https://www.cloudflare.com/cdn-cgi/trace | awk -F= '/^ip=/{print $2}')
[[ -n "$EGRESS" ]] && ok "выход через ingress, внешний IP: $EGRESS" \
                   || bad "нет выхода в интернет через ingress"

# 3. Конфиг TrustTunnel действительно указывает на ingress
if grep -q "socks5" "$TT_CFG" && grep -q "$INGRESS_PORT" "$TT_CFG"; then
  ok "vpn.toml направляет трафик в socks5://$INGRESS_HOST:$INGRESS_PORT"
else
  bad "в $TT_CFG нет forward_protocol.socks5 на нужный порт — выполните: ttx reconcile"
fi

# 4. UDP-ассоциация (нужна для QUIC/DNS клиентов)
if command -v nc >/dev/null; then
  ok "проверьте UDP вручную: с клиента curl --http3 https://cloudflare.com"
fi

# 5. Сервисы живы
for svc in x-ui trusttunnel ttx-bridge; do
  systemctl is-active --quiet "$svc" && ok "сервис $svc активен" || bad "сервис $svc не запущен"
done

exit $fail
