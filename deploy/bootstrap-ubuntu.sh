#!/usr/bin/env bash
# Полное автоматическое развёртывание ttx на чистом Ubuntu-сервере: от
# apt-пакетов до запущенных systemd-сервисов. Запускать НА сервере от root
# (локально или через deploy/deploy.sh + ssh).
#
# Что делает сам:
#   1. Проверяет, что это Ubuntu + systemd + root.
#   2. Ставит apt-зависимости, при необходимости — python3.11 отдельным
#      бинарём (штатный python3 дистрибутива не трогает).
#   3. (опционально, только по флагу) открывает 443/tcp+udp в ufw.
#   4. Запускает install/ttx-install.sh — он сам ставит 3x-ui и TrustTunnel
#      их штатными установщиками и раскладывает обвязку.
#   5. Копирует trusttunnel.service.template в systemd, если ещё не скопирован.
#   6. Берёт /opt/trusttunnel/vpn.toml (результат setup_wizard) как базовый
#      конфиг обвязки — либо использует файл, переданный через --vpn-base-toml.
#   7. Вписывает логин/пароль панели в /etc/ttx/bridge.json.
#   8. Прогоняет `ttx doctor` → `ttx reconcile` и включает сервисы.
#   9. (если не отключено) гоняет tests/e2e-smoke.sh.
#
# Что НЕ делает сам, потому что это не наш контракт (см. ARCHITECTURE.md):
#   - Не запускает `setup_wizard` TrustTunnel — это интерактивный мастер
#     upstream-проекта (сертификаты, домен, пользователи); мы не знаем его
#     будущих флагов и не будем их угадывать. Прогоните его один раз сами
#     (или на любом сервере и принесите готовый vpn.toml через
#     --vpn-base-toml) — дальше всё остальное скрипт сделает сам.
#
# Использование:
#   sudo ./deploy/bootstrap-ubuntu.sh \
#       --panel-user admin --panel-pass 'S3cr3t!' \
#       [--vpn-base-toml /root/vpn.toml] [опции]
#
# Без --panel-user/--panel-pass скрипт спросит их в терминале (пароль без
# эха); в неинтерактивном режиме (нет TTY) без этих флагов — упадёт с ошибкой.
# Пароль можно передать и через переменные окружения TTX_PANEL_USER/TTX_PANEL_PASS,
# чтобы не светить его в истории команд/списке процессов.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --------------------------------------------------------------------------- #
# Опции
# --------------------------------------------------------------------------- #

PANEL_USER="${TTX_PANEL_USER:-}"
PANEL_PASS="${TTX_PANEL_PASS:-}"
PANEL_BASE_URL=""
PANEL_BASE_PATH=""
INGRESS_PORT=""
VPN_BASE_TOML=""
TT_VERSION=""
XUI_VERSION=""
CONFIGURE_FIREWALL=0
ALLOW_PPA=0
SKIP_SMOKE_TEST=0
FORCE=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Использование: sudo ./deploy/bootstrap-ubuntu.sh [опции]

Учётные данные панели (обязательны — флагом, env или интерактивно):
  --panel-user <user>       логин 3x-ui (или env TTX_PANEL_USER)
  --panel-pass <pass>       пароль 3x-ui (или env TTX_PANEL_PASS)

Конфигурация:
  --panel-base-url <url>    адрес панели, если сменён (по умолчанию из bridge.example.json)
  --panel-base-path <path>  базовый путь панели, если сменён
  --ingress-port <port>     порт ingress-inbound (по умолчанию 10800)
  --vpn-base-toml <path>    готовый vpn.toml/vpn.base.toml (например, с другого сервера
                             после setup_wizard) — будет скопирован как базовый конфиг.
                             Без этого флага скрипт ждёт, что setup_wizard уже прогнан
                             локально и /opt/trusttunnel/vpn.toml существует.
  --tt-version <version>    версия TrustTunnel (передаётся install/ttx-install.sh)
  --xui-version <version>   версия 3x-ui

Прочее:
  --configure-firewall      добавить ufw-правила для 443/tcp+udp (см. предупреждение в коде)
  --allow-ppa                разрешить добавить deadsnakes PPA, если python3.11
                             недоступен из штатных репозиториев (по умолчанию запрещено)
  --skip-smoke-test          не гонять tests/e2e-smoke.sh в конце
  --force                    перезаписать уже существующие trusttunnel.service
                             и /etc/ttx/vpn.base.toml (по умолчанию скрипт их не трогает,
                             если они уже есть — чтобы не затереть ручные правки)
  -y, --yes                  не спрашивать финальное подтверждение перед стартом сервисов
  -h, --help                  эта справка
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --panel-user) PANEL_USER="${2:?}"; shift 2 ;;
    --panel-pass) PANEL_PASS="${2:?}"; shift 2 ;;
    --panel-base-url) PANEL_BASE_URL="${2:?}"; shift 2 ;;
    --panel-base-path) PANEL_BASE_PATH="${2:?}"; shift 2 ;;
    --ingress-port) INGRESS_PORT="${2:?}"; shift 2 ;;
    --vpn-base-toml) VPN_BASE_TOML="${2:?}"; shift 2 ;;
    --tt-version) TT_VERSION="${2:?}"; shift 2 ;;
    --xui-version) XUI_VERSION="${2:?}"; shift 2 ;;
    --configure-firewall) CONFIGURE_FIREWALL=1; shift ;;
    --allow-ppa) ALLOW_PPA=1; shift ;;
    --skip-smoke-test) SKIP_SMOKE_TEST=1; shift ;;
    --force) FORCE=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "неизвестный аргумент: $1" >&2; usage; exit 1 ;;
  esac
done

log()  { printf '\033[1;36m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[bootstrap] %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 1. Preflight
# --------------------------------------------------------------------------- #

[[ $EUID -eq 0 ]] || die "запускать от root (sudo)"

[[ -r /etc/os-release ]] || die "не нашёл /etc/os-release — это точно Linux?"
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "скрипт рассчитан на Ubuntu, обнаружено: ${PRETTY_NAME:-неизвестно}"
log "Ubuntu ${VERSION_ID:-?} (${VERSION_CODENAME:-?}) — ${PRETTY_NAME:-}"

command -v systemctl >/dev/null || die "нужен systemd"

[[ -x "$REPO_ROOT/install/ttx-install.sh" ]] \
  || die "не нашёл install/ttx-install.sh рядом со скриптом — запускайте из корня репозитория ttx"

# --------------------------------------------------------------------------- #
# 2. Учётные данные панели
# --------------------------------------------------------------------------- #

if [[ -z "$PANEL_USER" ]]; then
  if [[ -t 0 ]]; then
    read -r -p "Логин панели 3x-ui: " PANEL_USER
  else
    die "не задан логин панели — передайте --panel-user или TTX_PANEL_USER (нет TTY для запроса)"
  fi
fi
[[ -n "$PANEL_USER" ]] || die "логин панели пустой"

if [[ -z "$PANEL_PASS" ]]; then
  if [[ -t 0 ]]; then
    read -r -s -p "Пароль панели 3x-ui: " PANEL_PASS; echo
  else
    die "не задан пароль панели — передайте --panel-pass или TTX_PANEL_PASS (нет TTY для запроса)"
  fi
fi
[[ -n "$PANEL_PASS" ]] || die "пароль панели пустой"

# --------------------------------------------------------------------------- #
# 3. apt-зависимости + python3 >= 3.11
# --------------------------------------------------------------------------- #

log "apt-get update"
DEBIAN_FRONTEND=noninteractive apt-get update -qq

log "устанавливаю базовые пакеты (curl, ca-certificates, python3)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates python3 python3-minimal >/dev/null

python_ok() { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

PYBIN="/usr/bin/python3"
if python_ok "$PYBIN"; then
  log "системный python3 уже >= 3.11 ($("$PYBIN" --version 2>&1))"
else
  warn "системный python3 ($("$PYBIN" --version 2>&1)) старше 3.11 — нужен отдельный интерпретер для ttx"
  if command -v python3.11 >/dev/null && python_ok "$(command -v python3.11)"; then
    PYBIN="$(command -v python3.11)"
    log "нашёл готовый $PYBIN"
  else
    log "пробую поставить python3.11 из штатных репозиториев (штатный python3 не трогаю)"
    if DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.11 >/dev/null 2>&1; then
      PYBIN="$(command -v python3.11)"
      log "поставил $PYBIN"
    elif [[ $ALLOW_PPA -eq 1 ]]; then
      warn "python3.11 недоступен из штатных репозиториев — добавляю ppa:deadsnakes/ppa (--allow-ppa)"
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq software-properties-common >/dev/null
      add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
      DEBIAN_FRONTEND=noninteractive apt-get update -qq
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.11 >/dev/null
      PYBIN="$(command -v python3.11)"
      log "поставил $PYBIN через deadsnakes PPA"
    else
      die "python3.11 недоступен из штатных репозиториев этой Ubuntu. Поставьте вручную (например," \
          "через ppa:deadsnakes/ppa) или перезапустите с --allow-ppa, чтобы это сделал скрипт"
    fi
  fi
fi
python_ok "$PYBIN" || die "не удалось получить рабочий python3 >= 3.11 ($PYBIN)"

# --------------------------------------------------------------------------- #
# 4. Firewall (по умолчанию выключено)
# --------------------------------------------------------------------------- #

if [[ $CONFIGURE_FIREWALL -eq 1 ]]; then
  log "настраиваю ufw: 443/tcp+udp для TrustTunnel"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw >/dev/null

  # Не открываем себе путь к самоблокировке: сначала разрешаем текущий SSH-порт.
  SSH_PORT="$(ss -ltnp 2>/dev/null | awk '/sshd/ {print $4}' | sed 's/.*://' | head -n1)"
  SSH_PORT="${SSH_PORT:-22}"
  ufw allow "${SSH_PORT}/tcp" comment 'ssh (bootstrap-ubuntu.sh)' >/dev/null
  ufw allow 443/tcp comment 'ttx: trusttunnel' >/dev/null
  ufw allow 443/udp comment 'ttx: trusttunnel quic' >/dev/null

  if ufw status | grep -q "Status: active"; then
    log "ufw уже активен — правила добавлены"
  else
    warn "включаю ufw (правило для SSH-порта $SSH_PORT уже добавлено)"
    ufw --force enable >/dev/null
  fi
else
  log "--configure-firewall не задан — firewall не трогаю (порт 443/tcp+udp откройте сами при необходимости)"
fi

# --------------------------------------------------------------------------- #
# 5. Установка 3x-ui + TrustTunnel + обвязки (штатный install-скрипт)
# --------------------------------------------------------------------------- #

log "запускаю install/ttx-install.sh"
TT_VERSION="$TT_VERSION" XUI_VERSION="$XUI_VERSION" "$REPO_ROOT/install/ttx-install.sh"

TT_DIR="${TT_DIR:-/opt/trusttunnel}"
TTX_ETC="${TTX_ETC:-/etc/ttx}"

# Перецепляем CLI-обёртку ttx на найденный python >= 3.11, если системный python3 не годится.
if [[ "$PYBIN" != "/usr/bin/python3" && -f /usr/local/bin/ttx ]]; then
  sed -i "s#/usr/bin/python3#${PYBIN}#" /usr/local/bin/ttx
  log "/usr/local/bin/ttx теперь использует $PYBIN"
fi

# --------------------------------------------------------------------------- #
# 6. systemd-юнит TrustTunnel (из template, если ещё не скопирован)
# --------------------------------------------------------------------------- #

if [[ -f /etc/systemd/system/trusttunnel.service && $FORCE -eq 0 ]]; then
  log "/etc/systemd/system/trusttunnel.service уже существует — не трогаю (--force для перезаписи)"
elif [[ -f "$TT_DIR/trusttunnel.service.template" ]]; then
  cp "$TT_DIR/trusttunnel.service.template" /etc/systemd/system/trusttunnel.service
  log "установлен юнит trusttunnel.service из шаблона"
else
  die "нет $TT_DIR/trusttunnel.service.template — установка TrustTunnel не завершилась штатно"
fi

# --------------------------------------------------------------------------- #
# 7. Базовый конфиг TrustTunnel (vpn.base.toml)
# --------------------------------------------------------------------------- #

if [[ -f "$TTX_ETC/vpn.base.toml" && $FORCE -eq 0 && -z "$VPN_BASE_TOML" ]]; then
  log "$TTX_ETC/vpn.base.toml уже существует — не трогаю (--force или --vpn-base-toml для замены)"
elif [[ -n "$VPN_BASE_TOML" ]]; then
  [[ -f "$VPN_BASE_TOML" ]] || die "--vpn-base-toml: файл не найден: $VPN_BASE_TOML"
  install -m 0600 "$VPN_BASE_TOML" "$TTX_ETC/vpn.base.toml"
  log "базовый конфиг взят из $VPN_BASE_TOML"
elif [[ -f "$TT_DIR/vpn.toml" ]]; then
  install -m 0600 "$TT_DIR/vpn.toml" "$TTX_ETC/vpn.base.toml"
  log "базовый конфиг взят из $TT_DIR/vpn.toml (результат setup_wizard)"
else
  die "нет ни $TT_DIR/vpn.toml, ни --vpn-base-toml. Прогоните мастер TrustTunnel один раз:" \
      $'\n  '"cd $TT_DIR && sudo ./setup_wizard" \
      $'\n'"и перезапустите этот скрипт — либо принесите готовый файл через --vpn-base-toml."
fi

# --------------------------------------------------------------------------- #
# 8. bridge.json — доступы к панели и опциональные оверрайды
# --------------------------------------------------------------------------- #

log "вписываю доступы к панели в $TTX_ETC/bridge.json"
PANEL_USER="$PANEL_USER" PANEL_PASS="$PANEL_PASS" \
PANEL_BASE_URL="$PANEL_BASE_URL" PANEL_BASE_PATH="$PANEL_BASE_PATH" \
INGRESS_PORT="$INGRESS_PORT" BRIDGE_JSON="$TTX_ETC/bridge.json" \
"$PYBIN" - <<'PY'
import json, os

path = os.environ["BRIDGE_JSON"]
with open(path, encoding="utf-8") as fh:
    cfg = json.load(fh)

cfg.setdefault("panel", {})
cfg["panel"]["username"] = os.environ["PANEL_USER"]
cfg["panel"]["password"] = os.environ["PANEL_PASS"]
if os.environ.get("PANEL_BASE_URL"):
    cfg["panel"]["base_url"] = os.environ["PANEL_BASE_URL"]
if os.environ.get("PANEL_BASE_PATH"):
    cfg["panel"]["base_path"] = os.environ["PANEL_BASE_PATH"]

if os.environ.get("INGRESS_PORT"):
    cfg.setdefault("ingress", {})
    cfg["ingress"]["port"] = int(os.environ["INGRESS_PORT"])

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, path)
os.chmod(path, 0o600)
PY
log "bridge.json обновлён (права 0600)"

# --------------------------------------------------------------------------- #
# 9. doctor -> reconcile -> enable
# --------------------------------------------------------------------------- #

log "ttx doctor"
if ! /usr/local/bin/ttx doctor; then
  die "ttx doctor нашёл проблемы — см. вывод выше. Поправьте $TTX_ETC/bridge.json / vpn.base.toml и перезапустите скрипт"
fi

log "ttx reconcile --dry-run (предпросмотр)"
/usr/local/bin/ttx reconcile --dry-run

if [[ $ASSUME_YES -eq 0 && -t 0 ]]; then
  read -r -p "Применить конфигурацию и запустить сервисы? [y/N] " CONFIRM
  [[ "$CONFIRM" =~ ^[Yy]$ ]] || die "остановлено пользователем"
fi

log "ttx reconcile"
/usr/local/bin/ttx reconcile

log "systemctl daemon-reload + enable --now x-ui trusttunnel ttx-bridge"
systemctl daemon-reload
systemctl enable --now x-ui trusttunnel ttx-bridge

# --------------------------------------------------------------------------- #
# 10. Смоук-тест и итог
# --------------------------------------------------------------------------- #

if [[ $SKIP_SMOKE_TEST -eq 0 ]]; then
  log "sleep 3 && tests/e2e-smoke.sh"
  sleep 3
  if ! /opt/ttx/tests/e2e-smoke.sh; then
    warn "e2e-smoke.sh нашёл проблемы — см. вывод выше и RUNBOOK.md"
  fi
else
  log "--skip-smoke-test: пропускаю"
fi

echo
log "готово. Текущее состояние:"
/usr/local/bin/ttx status || true

cat <<EOF

Дальше вручную (не автоматизировано умышленно):
  - Выдача клиентских конфигов TrustTunnel — см. README.md, раздел «Выдача клиента».
  - journalctl -u ttx-bridge -f    — если что-то пойдёт не так, смотрите сюда и в RUNBOOK.md.
EOF
