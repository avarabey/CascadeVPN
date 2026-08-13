#!/usr/bin/env bash
# Установщик обвязки. Исходные проекты ставятся ИХ ЖЕ штатными установщиками,
# ни один их файл не патчится. Мы добавляем только /opt/ttx, /etc/ttx и drop-in.
set -euo pipefail

TT_DIR="${TT_DIR:-/opt/trusttunnel}"
TTX_DIR="${TTX_DIR:-/opt/ttx}"
TTX_ETC="${TTX_ETC:-/etc/ttx}"
TT_VERSION="${TT_VERSION:-}"          # пусто = последняя
XUI_VERSION="${XUI_VERSION:-}"        # пусто = последняя
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\033[1;36m[ttx]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[ttx] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запускать от root"
command -v python3 >/dev/null || die "нужен python3 >= 3.11"

step_xui() {
  if command -v x-ui >/dev/null; then log "3x-ui уже установлен — пропускаю"; return; fi
  log "устанавливаю 3x-ui (upstream installer, без модификаций)"
  if [[ -n "$XUI_VERSION" ]]; then
    bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) "$XUI_VERSION"
  else
    bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)
  fi
}

step_trusttunnel() {
  if [[ -x "$TT_DIR/trusttunnel_endpoint" ]]; then
    log "TrustTunnel уже установлен — пропускаю"; return
  fi
  log "устанавливаю TrustTunnel endpoint (upstream installer, без модификаций)"
  if [[ -n "$TT_VERSION" ]]; then
    curl -fsSL https://raw.githubusercontent.com/TrustTunnel/TrustTunnel/refs/heads/master/scripts/install.sh \
      | sh -s - -o "$TT_DIR" -V "$TT_VERSION"
  else
    curl -fsSL https://raw.githubusercontent.com/TrustTunnel/TrustTunnel/refs/heads/master/scripts/install.sh \
      | sh -s - -o "$TT_DIR"
  fi
  log "запустите мастер: cd $TT_DIR && sudo ./setup_wizard"
  log "юнит: cp $TT_DIR/trusttunnel.service.template /etc/systemd/system/trusttunnel.service"
}

step_overlay() {
  log "разворачиваю обвязку в $TTX_DIR"
  install -d "$TTX_DIR/bridge" "$TTX_ETC" "$TTX_DIR/tests"
  install -m 0755 "$SRC/bridge/ttx_bridge.py" "$TTX_DIR/bridge/ttx_bridge.py"
  install -m 0755 "$SRC/tests/e2e-smoke.sh"   "$TTX_DIR/tests/e2e-smoke.sh"
  install -m 0644 "$SRC/templates/compat.json" "$TTX_ETC/compat.json"

  [[ -f "$TTX_ETC/bridge.json" ]] || {
    install -m 0600 "$SRC/bridge/bridge.example.json" "$TTX_ETC/bridge.json"
    log "создан $TTX_ETC/bridge.json — впишите логин/пароль панели"
  }

  # Базовый конфиг: если оператор уже прогнал setup_wizard, забираем его вывод.
  if [[ ! -f "$TTX_ETC/vpn.base.toml" ]]; then
    if [[ -f "$TT_DIR/vpn.toml" ]]; then
      cp "$TT_DIR/vpn.toml" "$TTX_ETC/vpn.base.toml"
      log "базовый конфиг взят из $TT_DIR/vpn.toml"
    else
      cp "$SRC/templates/vpn.base.toml.example" "$TTX_ETC/vpn.base.toml"
      log "положен пример базового конфига — отредактируйте $TTX_ETC/vpn.base.toml"
    fi
  fi

  install -d /etc/systemd/system/trusttunnel.service.d
  install -m 0644 "$SRC/systemd/trusttunnel.service.d/10-ttx-overlay.conf" \
                  /etc/systemd/system/trusttunnel.service.d/10-ttx-overlay.conf
  for u in ttx-bridge.service ttx-reconcile.service ttx-reconcile.timer; do
    install -m 0644 "$SRC/systemd/$u" "/etc/systemd/system/$u"
  done
  systemctl daemon-reload
  log "готово. Дальше: ttx doctor → ttx reconcile → systemctl enable --now ttx-bridge"
}

step_cli() {
  cat > /usr/local/bin/ttx <<'CLI'
#!/usr/bin/env bash
exec /usr/bin/python3 /opt/ttx/bridge/ttx_bridge.py "$@" -c "${TTX_CONFIG:-/etc/ttx/bridge.json}"
CLI
  chmod 0755 /usr/local/bin/ttx
}

step_xui
step_trusttunnel
step_overlay
step_cli
