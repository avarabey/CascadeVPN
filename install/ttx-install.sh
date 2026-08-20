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
PYTHON_BIN="${PYTHON_BIN:-python3}"

log() { printf '\033[1;36m[ttx]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[ttx] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запускать от root"
command -v "$PYTHON_BIN" >/dev/null || die "не найден Python: $PYTHON_BIN"
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "нужен python3 >= 3.11"
PYTHON_BIN_PATH="$(command -v "$PYTHON_BIN")"

portal_hash_configured() {
  [[ -r "$TTX_ETC/portal.env" ]] || return 1
  awk '
    /^PORTAL_PASSWORD_HASH=/ {
      value = substr($0, length("PORTAL_PASSWORD_HASH=") + 1)
      sub(/[[:space:]]+$/, "", value)
      seen = 1
    }
    END {
      fields = split(value, part, ":")
      valid = seen && fields == 4 \
        && part[1] == "pbkdf2_sha256" \
        && part[2] ~ /^[0-9]+$/ \
        && part[2] >= 100000 && part[2] <= 10000000 \
        && part[3] ~ /^[A-Za-z0-9_-]+$/ && length(part[3]) >= 22 \
        && part[4] ~ /^[A-Za-z0-9_-]+$/ && length(part[4]) == 43
      exit(valid ? 0 : 1)
    }
  ' "$TTX_ETC/portal.env"
}

step_xui() {
  if command -v x-ui >/dev/null; then log "3x-ui уже установлен — пропускаю"; return; fi
  log "устанавливаю 3x-ui (upstream installer, без модификаций)"
  if [[ -n "$XUI_VERSION" ]]; then
    bash <(curl -fsSL https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) "$XUI_VERSION"
  else
    bash <(curl -fsSL https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)
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
    log "создан $TTX_ETC/bridge.json — впишите API token или логин/пароль панели"
  }

  # Базовый конфиг: если оператор уже прогнал setup_wizard, забираем его вывод.
  if [[ ! -f "$TTX_ETC/vpn.base.toml" ]]; then
    if [[ -f "$TT_DIR/vpn.toml" ]]; then
      install -m 0600 "$TT_DIR/vpn.toml" "$TTX_ETC/vpn.base.toml"
      log "базовый конфиг взят из $TT_DIR/vpn.toml"
    else
      install -m 0600 "$SRC/templates/vpn.base.toml.example" "$TTX_ETC/vpn.base.toml.example"
      log "vpn.toml ещё не создан; пример лежит в $TTX_ETC/vpn.base.toml.example"
      log "сначала выполните: cd $TT_DIR && sudo ./setup_wizard"
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

step_portal() {
  [[ -f "$SRC/portal/app/__main__.py" ]] \
    || { log "portal ещё не собран в исходниках — пропускаю"; return; }

  log "разворачиваю личный портал в $TTX_DIR/portal"
  install -d -m 0755 "$TTX_DIR/portal"
  tar --exclude='__pycache__' --exclude='*.py[co]' --exclude='.pytest_cache' \
      -C "$SRC/portal" -cf - . | tar -C "$TTX_DIR/portal" -xf -

  if ! getent group ttx-portal >/dev/null; then
    groupadd --system ttx-portal
  fi
  if ! id -u ttx-portal >/dev/null 2>&1; then
    useradd --system --gid ttx-portal --home-dir /nonexistent \
      --shell /usr/sbin/nologin ttx-portal
  fi
  install -d -o ttx-portal -g ttx-portal -m 0700 /var/lib/ffknd-portal

  install -m 0644 "$SRC/systemd/ffknd-portal.service" \
                  /etc/systemd/system/ffknd-portal.service
  if [[ "$PYTHON_BIN_PATH" != "/usr/bin/python3" ]]; then
    sed -i "s#ExecStart=/usr/bin/python3#ExecStart=${PYTHON_BIN_PATH}#" \
      /etc/systemd/system/ffknd-portal.service
  fi
  if [[ -f "$SRC/portal/portal.env.example" ]]; then
    install -m 0600 "$SRC/portal/portal.env.example" \
                    "$TTX_ETC/portal.env.example"
  fi

  # Не захватываем уже настроенный оператором reverse proxy. Если секции нет,
  # добавляем помеченный нами loopback-origin атомарно. Snapshot
  # остаётся для аварийного разбора; штатный rollback удаляет только наш блок.
  if [[ -f "$TTX_ETC/vpn.base.toml" ]]; then
    "$PYTHON_BIN_PATH" "$SRC/portal/configure_reverse_proxy.py" add \
      "$TTX_ETC/vpn.base.toml"
  fi

  cat > /usr/local/bin/ffknd-portal-config <<CLI
#!/usr/bin/env bash
exec "$PYTHON_BIN_PATH" "$TTX_DIR/portal/configure_reverse_proxy.py" "\$@"
CLI
  chmod 0755 /usr/local/bin/ffknd-portal-config

  systemctl daemon-reload
  if ! portal_hash_configured \
     && { systemctl is-active --quiet ffknd-portal \
          || systemctl is-enabled --quiet ffknd-portal; }; then
    log "отключаю ffknd-portal до настройки PORTAL_PASSWORD_HASH"
    systemctl disable --now ffknd-portal \
      || die "не удалось безопасно остановить ffknd-portal"
  fi
  log "portal установлен; установщик не запускает его автоматически"
  log "создайте хеш: cd $TTX_DIR/portal && $PYTHON_BIN_PATH -m app hash-password"
  log "сохраните PORTAL_PASSWORD_HASH в $TTX_ETC/portal.env и включите ffknd-portal"
  log "rollback reverse proxy: ffknd-portal-config remove $TTX_ETC/vpn.base.toml"
}

step_cli() {
  cat > /usr/local/bin/ttx <<CLI
#!/usr/bin/env bash
exec "$PYTHON_BIN_PATH" /opt/ttx/bridge/ttx_bridge.py "\$@" -c "\${TTX_CONFIG:-/etc/ttx/bridge.json}"
CLI
  chmod 0755 /usr/local/bin/ttx
}

step_xui
step_trusttunnel
step_overlay
step_portal
step_cli
