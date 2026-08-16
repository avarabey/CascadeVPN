#!/usr/bin/env bash
# Разворачивает обвязку CascadeVPN (ttx) на удалённом сервере по SSH:
# синхронизирует репозиторий rsync'ом и (если не указано --skip-install)
# запускает install/ttx-install.sh на сервере.
#
# Секреты (bridge.json, vpn.base.toml, сертификаты) НЕ трогает и НЕ
# перезаписывает на сервере — они исключены из синхронизации явно, чтобы
# не затереть то, что оператор уже настроил на месте. Их разворачивание —
# отдельный ручной шаг (см. вывод скрипта в конце) либо задача №1 в TODO.
#
# Использование:
#   ./deploy/deploy.sh user@host [--branch main] [--remote-dir ttx-src]
#                                 [--skip-install] [--dry-run]
#
# Требования на клиенте: ssh, rsync, (опционально) git — для проверки ветки.
# Требования на сервере:  python3 >= 3.11, systemd, root или sudo по SSH.

set -euo pipefail

usage() {
  cat <<'EOF'
Использование: deploy.sh user@host [опции]

Опции:
  --branch <name>       требовать, чтобы локально была выбрана именно эта
                         git-ветка/тег перед деплоем (по умолчанию: без проверки)
  --remote-dir <path>   куда класть исходники на сервере (по умолчанию: ttx-src в home)
  --skip-install        только синхронизировать файлы, install/ttx-install.sh не запускать
  --dry-run             показать, что будет сделано (rsync --dry-run, установка не запускается)
  -h, --help             эта справка
EOF
}

HOST=""
BRANCH=""
REMOTE_DIR="ttx-src"
SKIP_INSTALL=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch) BRANCH="${2:?--branch требует значение}"; shift 2 ;;
    --remote-dir) REMOTE_DIR="${2:?--remote-dir требует значение}"; shift 2 ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "неизвестная опция: $1" >&2; usage; exit 1 ;;
    *)
      if [[ -z "$HOST" ]]; then HOST="$1"; shift
      else echo "лишний аргумент: $1" >&2; usage; exit 1
      fi
      ;;
  esac
done

[[ -n "$HOST" ]] || { usage; exit 1; }

log()  { printf '\033[1;36m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[deploy] %s\033[0m\n' "$*" >&2; exit 1; }

command -v rsync >/dev/null || die "нужен rsync"
command -v ssh    >/dev/null || die "нужен ssh"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "$BRANCH" ]]; then
  command -v git >/dev/null || die "--branch требует git в PATH"
  CURRENT_BRANCH="$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  [[ "$CURRENT_BRANCH" == "$BRANCH" ]] \
    || die "локально выбрана ветка '$CURRENT_BRANCH', а не '$BRANCH' — переключитесь: git -C '$SRC_DIR' checkout '$BRANCH'"
fi

log "источник: $SRC_DIR"
log "цель:     $HOST:$REMOTE_DIR"

# Секреты и локальные артефакты в целевой каталог не копируем — они должны
# жить только на сервере (/etc/ttx/*) и настраиваться там вручную.
RSYNC_OPTS=(-az --delete
  --exclude ".git/"
  --exclude ".DS_Store"
  --exclude "compose/.env"
  --exclude "compose/bridge.json"
  --exclude "compose/vpn.base.toml"
  --exclude "compose/certs/"
  --exclude "*.ttx-bak"
  --exclude "__pycache__/")
[[ $DRY_RUN -eq 1 ]] && RSYNC_OPTS+=(--dry-run -v)

rsync "${RSYNC_OPTS[@]}" "$SRC_DIR"/ "$HOST:$REMOTE_DIR/"

if [[ $SKIP_INSTALL -eq 1 ]]; then
  log "--skip-install: файлы синхронизированы, установка пропущена"
  exit 0
fi

INSTALL_CMD="cd '$REMOTE_DIR' && sudo ./install/ttx-install.sh"
if [[ $DRY_RUN -eq 1 ]]; then
  log "(dry-run) на сервере выполнилось бы: $INSTALL_CMD"
  exit 0
fi

log "запускаю установку на сервере ($HOST)..."
ssh -t "$HOST" "$INSTALL_CMD"

cat <<EOF

$(log "код на сервере развёрнут. Дальше — вручную на \$HOST:")
  sudo nano /etc/ttx/bridge.json         # доступы к панели 3x-ui
  sudo nano /etc/ttx/vpn.base.toml       # базовый конфиг TrustTunnel (если ещё не из setup_wizard)
  sudo ttx doctor
  sudo ttx reconcile --dry-run           # посмотреть, что изменится
  sudo ttx reconcile
  sudo systemctl daemon-reload
  sudo systemctl enable --now x-ui trusttunnel ttx-bridge
  sudo /opt/ttx/tests/e2e-smoke.sh
EOF
