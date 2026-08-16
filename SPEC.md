# SPEC — CascadeVPN (ttx)

Живой документ. Обновляется по ходу работы над проектом; в конце — журнал
изменений. Дублирует часть [README.md](README.md) и [ARCHITECTURE.md](ARCHITECTURE.md),
но собирает всё в одном месте и добавляет то, что в них не поместилось:
точную схему конфигурации, карту репозитория, эксплуатационные детали.

## 1. Назначение и рамки проекта

**Задача:** дать трафику TrustTunnel (endpoint, маскирующийся под HTTPS)
попасть под управление панели 3x-ui (Xray) — маршрутизация, гео-правила,
outbound'ы (direct/WARP/VLESS-цепочки/blackhole) — без форка и без патчинга
исходников обоих upstream-проектов.

**Не является целью проекта:**
- Замена или обёртка функциональности 3x-ui/TrustTunnel по отдельности —
  оба ставятся и работают их штатными средствами.
- Пофамильный биллинг/квоты на уровне ingress'а (см. §9, «Учёт трафика» —
  known limitation, а не фича в разработке).
- Поддержка версий 3x-ui/TrustTunnel вне матрицы `templates/compat.json`.

**Единственный runtime-код проекта** — `bridge/ttx_bridge.py`, демон-медиатор
("мост") на стандартной библиотеке Python 3.9+, без внешних зависимостей.
Всё остальное — конфигурация, systemd-юниты, инсталлятор, тесты.

## 2. Архитектура (кратко)

```
клиент TrustTunnel
      │ TLS/HTTP2/QUIC :443            (неотличимо от HTTPS)
      ▼
trusttunnel_endpoint  [forward_protocol.socks5]  ◄── генерирует ttx-bridge
      │ SOCKS5 (TCP + UDP ASSOCIATE) → 127.0.0.1:10800
      ▼
Xray inbound "TTX-Ingress" (создан и поддерживается через REST API 3x-ui)
      │
      ▼  routing 3x-ui: правила, geoip/geosite, балансировщики
   outbound: direct / WARP / VLESS-цепочка / blackhole
```

Полное обоснование выбора этой точки стыковки (`forward_protocol.socks5`
против reverse-proxy, iptables-перехвата и форка) — в
[ARCHITECTURE.md](ARCHITECTURE.md), AD-1.

## 3. Карта репозитория

```
CascadeVPN/
├── README.md                 — обзор, установка, эксплуатация (для оператора)
├── ARCHITECTURE.md           — ADR: почему так, а не иначе
├── RUNBOOK.md                — диагностика инцидентов
├── SPEC.md                   — этот файл: детальная спецификация
├── bridge/
│   ├── ttx_bridge.py         — демон-мост (CLI: reconcile|watch|status|doctor)
│   ├── bridge.example.json   — образец конфига моста (→ /etc/ttx/bridge.json)
│   └── Dockerfile            — минимальный образ bridge на Python 3.12
├── install/
│   └── ttx-install.sh        — ставит 3x-ui + TrustTunnel + обвязку (bare-metal)
├── templates/
│   ├── compat.json           — матрица min/tested версий upstream (→ /etc/ttx/compat.json)
│   └── vpn.base.toml.example — образец базового конфига TrustTunnel
├── systemd/
│   ├── trusttunnel.service.d/10-ttx-overlay.conf  — drop-in к юниту TrustTunnel
│   ├── ttx-bridge.service     — постоянный демон (`ttx watch`), основной режим
│   ├── ttx-reconcile.service  — разовый reconcile
│   └── ttx-reconcile.timer    — таймер-альтернатива watch-режиму
├── tests/
│   ├── check.sh               — локальная проверка синтаксиса, JSON и unit-тестов
│   ├── test_bridge.py         — unit-тесты рендера, ownership и защиты от петель
│   └── e2e-smoke.sh           — сквозная проверка на установленном сервере
├── compose/
│   ├── docker-compose.yml     — x-ui + trusttunnel (build из upstream по тегу) + bridge
│   ├── .env.example           — XUI_TAG, TT_VERSION, XUI_BASE_PATH, XUI_PANEL_PORT
│   ├── bridge.docker.json     — образец bridge.json под docker-сеть
│   ├── vpn.base.toml.example  — образец базового конфига под контейнер (порт 8443)
│   ├── credentials.toml.example — пример пользователей TrustTunnel
│   ├── hosts.toml.example       — пример TLS-хоста TrustTunnel
│   └── rules.toml.example       — пример файла правил TrustTunnel
└── deploy/
    ├── deploy.sh               — rsync+ssh деплой репозитория на сервер + install
    └── bootstrap-ubuntu.sh     — полное автоматическое развёртывание НА сервере
                                  (Ubuntu): apt-зависимости → install/ttx-install.sh →
                                  bridge.json → doctor/reconcile → systemd enable → smoke-test
```

Все пути внутри `install/ttx-install.sh` (`$SRC/bridge/...`,
`$SRC/templates/...`, `$SRC/systemd/...`, `$SRC/tests/...`) рассчитаны
именно на эту раскладку: `SRC` вычисляется как родитель каталога `install/`.

## 4. Конфигурация моста — `bridge.json`

Путь по умолчанию: `/etc/ttx/bridge.json` (переопределяется `-c/--config`
или переменной `TTX_CONFIG`). Схема (все поля из `Config.__init__`,
`bridge/ttx_bridge.py`):

| Ключ | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `panel.base_url` | str | `http://127.0.0.1:2053` | адрес панели 3x-ui |
| `panel.base_path` | str | `/` | базовый путь панели, если сменён |
| `panel.api_token` | str | `""` | Bearer token панели; рекомендуемый машинный способ аутентификации |
| `panel.username` | str | `""` | логин панели; обязателен вместе с password, если api_token пуст |
| `panel.password` | str | `""` | пароль панели; обязателен вместе с username, если api_token пуст |
| `panel.verify_tls` | bool | `true` | проверять TLS-сертификат панели |
| `ingress.remark` | str | `TTX-Ingress` | метка inbound'а, по которой мост его находит |
| `ingress.listen` | str | `127.0.0.1` | адрес, на котором слушает ingress-inbound |
| `ingress.port` | int | `10800` | порт ingress-inbound |
| `ingress.protocol` | str | `socks` | `socks` \| `mixed` |
| `ingress.udp` | bool | `true` | нужно для QUIC/DNS через UDP ASSOCIATE |
| `ingress.sniffing` | bool | `true` | sniffing destOverride http/tls/quic |
| `ingress.manage` | bool | `true` | `false` = панель источник правды, мост только читает |
| `trusttunnel.base_config` | path | `/etc/ttx/vpn.base.toml` | базовый TOML оператора |
| `trusttunnel.target_config` | path | `/opt/trusttunnel/vpn.toml` | итоговый файл, который мост пишет |
| `trusttunnel.reach_host` | str | `""` (= `ingress.listen`) | как TrustTunnel видит ingress (важно в docker — алиас контейнера) |
| `trusttunnel.service` | str | `trusttunnel.service` | имя systemd-юнита для restart |
| `trusttunnel.binary` | path | `/opt/trusttunnel/trusttunnel_endpoint` | для `--version` в проверке совместимости |
| `trusttunnel.extended_auth` | bool | `false` | пишется в `[forward_protocol.socks5]` |
| `trusttunnel.restart_on_change` | bool | `true` | `false` в docker — систему рестартует `docker compose restart` снаружи |
| `compat.file` | path | `/etc/ttx/compat.json` | матрица совместимости |
| `compat.strict` | bool | `false` | `true` = отказ работать при несовпадении версии |
| `interval_secs` | int | `60` | период цикла в режиме `watch` |

Неизвестные ключи верхнего уровня (например, `_comment`) игнорируются —
это осознанное решение (AD-4, п.4), чтобы будущие поля не ломали парсинг.

## 5. Алгоритм `reconcile`

1. `check_compat()` — сверяет `trusttunnel_endpoint --version` с
   `compat.json`; при `strict=true` и несовпадении — выход с кодом 3.
2. Аутентификация в панели: Bearer API token либо `/csrf-token` → `/login`
   для cookie-сессии. Ingress ищется **только** по принадлежащему мосту
   `remark`; совпадение чужого порта не даёт мосту права захватывать inbound.
3. Если не найден и `manage=true` — создаётся (`/panel/api/inbounds/add`);
   если найден и разошёлся с эталоном (порт/enable/listen) — чинится
   (`/panel/api/inbounds/update/{id}`), либо только предупреждение при
   `manage=false`.
4. `render_vpn_config` — берёт `base_config` целиком, обрезает всё после
   маркера (safety net на случай если базой случайно стал уже отрендеренный
   файл), вырезает таблицу `[forward_protocol]` (если оператор её туда
   всё же вписал) и добавляет управляемую секцию с адресом ingress'а.
5. `guard_loop` — отказ (`SystemExit`), если порт ingress совпал с портом
   `listen_address` TrustTunnel (защита от петли трафика).
6. `write_if_changed` — атомарная запись (temp-файл + `os.replace`),
   предварительно бэкап в `<file>.ttx-bak`. Если контента не изменился —
   ничего не пишет и не перезапускает.
7. При изменении и `restart_on_change=true` — `systemctl restart <service>`.

Идемпотентность: повторный прогон над уже управляемым файлом даёт тот же
результат (маркер и секция не дублируются, см. `AD-5`).

## 6. CLI

```
ttx reconcile [--dry-run] [-v]   # один цикл согласования; --dry-run печатает
                                   бы-записанный конфиг, ничего не меняя
ttx reconcile --no-restart       # записать без systemctl restart; для ExecStartPre
ttx watch     [--dry-run] [-v]   # цикл reconcile каждые interval_secs (сервис по умолчанию)
ttx status                        # текущее состояние ingress + куда указывает vpn.toml
ttx doctor                        # предполётные проверки (см. ниже)
```

`ttx doctor` проверяет: существование `vpn.base.toml`, успешный логин в
панель, `allow_private_network_connections != true` в базовом конфиге.

## 7. Два режима работы демона

- **`ttx-bridge.service`** (по умолчанию, включается в README) — долгоживущий
  процесс, сам крутит цикл `watch` с интервалом `interval_secs`.
- **`ttx-reconcile.timer` + `ttx-reconcile.service`** — альтернатива на
  systemd-таймере (`OnUnitActiveSec=60s`) для тех, кто предпочитает
  штатный планировщик встроенному циклу. **Не включайте оба одновременно**
  на одном сервере — это лишняя нагрузка на API панели, без функциональной
  пользы.
- **`trusttunnel.service.d/10-ttx-overlay.conf`** — не альтернатива, а
  дополнение к любому из двух режимов: гарантирует свежий `vpn.toml`
  именно в момент (пере)запуска TrustTunnel через
  `ExecStartPre=ttx reconcile --no-restart`. Флаг исключает рекурсивный restart
  того же юнита из его pre-start hook.

## 8. Docker-вариант — отличия от bare-metal

- `x-ui` и `trusttunnel` — разные контейнеры в общей сети `ttx`; `bridge`
  использует `panel.base_url=http://xui:2053` (DNS-алиас), а
  `trusttunnel.reach_host=xui`, чтобы контейнер TrustTunnel мог достучаться
  до ingress-inbound по сети docker, а не по `127.0.0.1`.
- `ingress.listen=0.0.0.0` внутри контейнера x-ui (иначе Xray слушал бы
  loopback, недоступный извне контейнера).
- Общий named volume монтируется в штатный upstream-путь
  `/trusttunnel_endpoint`; итоговый `vpn.toml` пишет bridge, а
  `credentials.toml`, `hosts.toml` и `rules.toml` подключаются read-only.
- `trusttunnel.restart_on_change=false` — `systemctl` внутри контейнера
  недоступен; перезапуск контейнера при изменении `vpn.toml` — ручная
  операция (`docker compose restart trusttunnel`) либо задача на будущее
  (entrypoint с file-watcher, см. §11).
- Порты хоста: `443/tcp+udp → 8443` контейнера; `vpn.base.toml.example`
  для compose отражает это (`listen_address = "0.0.0.0:8443"`).

## 9. Безопасность и известные ограничения

Перенесено и расширено из README §5:

- **ICMP** не переносится SOCKS5 — при включённой `[icmp]` пинги идут в
  обход routing 3x-ui напрямую с сервера.
- **UDP** требует `udp: true` у inbound (мост ставит сам) и поддержки
  UDP ASSOCIATE клиентом endpoint'а — иначе QUIC/DNS деградируют на TCP.
- **Учёт трафика** через один ingress агрегированный для панели; пофамильный
  учёт — только на стороне TrustTunnel (`credentials.toml` + метрики
  Prometheus на `:1987`).
- **Перезапуск Xray** при любом изменении в панели на 1-2 секунды рвёт
  активные соединения через ingress (TLS-сессия клиента с endpoint'ом не рвётся).
- **Петли трафика**: `allow_private_network_connections` обязан быть
  `false` — проверяется `ttx doctor`; `guard_loop` отдельно отказывается
  писать конфиг, если порт ingress совпал с `listen_address`.
- **Секреты**: `bridge.json`, `vpn.base.toml`, `credentials.toml`, `hosts.toml`,
  `rules.toml`, `certs/` не должны попадать
  в git — исключены в `.gitignore`; `deploy/deploy.sh` явно исключает их
  из синхронизации на сервер, чтобы не затирать то, что уже настроено на месте.
- **Docker restart_on_change**: см. §8 — при отключённом рестарте изменения
  `vpn.toml` не подхватятся сами, пока кто-то не перезапустит контейнер.

## 10. Совместимость версий (см. также ARCHITECTURE.md AD-4)

`templates/compat.json` фиксирует `min_version`, `reference_version` и список
используемых API-эндпоинтов/фич. `reference_version` означает версию, по
официальной документации которой сверялся контракт, а не обещание полного
e2e-теста. Сейчас бинарная проверка версии реализована для TrustTunnel;
совместимость 3x-ui проверяется фактическими вызовами `/csrf-token`,
`/login` и `/panel/api/inbounds/*`. Клиент поддерживает Bearer API token и
cookie-сессию с CSRF, а также принимает как JSON-строки старого API, так и
вложенные JSON-объекты нового. При `compat.strict=true` несовпадение версии
TrustTunnel останавливает `reconcile`.

Внешние контракты, сверенные 2026-08-16:

- [TrustTunnel configuration](https://github.com/TrustTunnel/TrustTunnel/blob/master/CONFIGURATION.md)
  — `forward_protocol.socks5`, структура `vpn.toml`, metrics и отдельные
  `hosts.toml`/`credentials.toml`/`rules.toml`.
- [TrustTunnel Dockerfile.prebuilt](https://github.com/TrustTunnel/TrustTunnel/blob/master/Dockerfile.prebuilt)
  и [docker-entrypoint.sh](https://github.com/TrustTunnel/TrustTunnel/blob/master/docker-entrypoint.sh)
  — рабочий каталог `/trusttunnel_endpoint` и обязательные файлы контейнера.
- [3x-ui API authentication](https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/authentication.mdx)
  — Bearer token и cookie-сессия с CSRF.
- [3x-ui inbounds API](https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/inbounds.mdx)
  — list/add/update endpoints и формат полей inbound.

## 11. Развёртывание

- **Bare-metal, полностью автоматически (Ubuntu):**
  `sudo ./deploy/bootstrap-ubuntu.sh`
  на самом сервере. Делает всё от `apt-get update` до включённых
  `x-ui`/`trusttunnel`/`ttx-bridge` и `tests/e2e-smoke.sh` в конце:
  проверяет, что это Ubuntu+systemd+root; ставит apt-зависимости; при
  системном python3 < 3.9 ставит отдельно `python3.11` (штатный
  `/usr/bin/python3` не трогает, вместо этого переключает на него обёртку
  `/usr/local/bin/ttx`); опционально (`--configure-firewall`, по умолчанию
  выключено) открывает 443/tcp+udp в ufw, предварительно разрешив текущий
  SSH-порт, чтобы не заблокировать себе доступ; запускает
  `install/ttx-install.sh`; раскладывает `trusttunnel.service` из шаблона;
  берёт `/opt/trusttunnel/vpn.toml` (результат `setup_wizard`) или файл из
  `--vpn-base-toml` как базовый конфиг; для новой установки читает API token,
  порт и web path 3x-ui из root-only `/etc/x-ui/install-result.env`, а для
  существующей принимает `--panel-api-token` либо логин+пароль; затем пишет
  `bridge.json` и гоняет `doctor` → `reconcile` → `systemctl enable --now`.
  Идемпотентен: существующие `trusttunnel.service`/`vpn.base.toml` не
  перезаписывает без `--force`.

  **Сознательно не автоматизирует** только `setup_wizard` TrustTunnel —
  это интерактивный мастер апстрима (домен, сертификаты, пользователи);
  скрипт не угадывает его флаги, а останавливается с понятной инструкцией,
  если `/opt/trusttunnel/vpn.toml` ещё не создан и `--vpn-base-toml` не
  передан. Это тот же принцип «расширение, а не модификация/угадывание
  чужого контракта», что и в остальном проекте (см. ARCHITECTURE.md AD-1/AD-2).

- **Bare-metal, вручную по шагам:** `sudo ./install/ttx-install.sh` на самом
  сервере (см. README §3) — ставит upstream штатными инсталляторами и
  раскладывает обвязку в `/opt/ttx`, `/etc/ttx`; остальные шаги — руками
  (полезно для отладки или нестандартных конфигураций).
- **Bare-metal, удалённо, без полной автоматизации:** `./deploy/deploy.sh user@host` с клиентской
  машины — синхронизирует репозиторий по rsync и по SSH запускает
  `install/ttx-install.sh` на сервере. Секреты не копирует (см. §9) —
  их оператор заполняет на сервере вручную после деплоя (шаги печатаются
  в конце работы скрипта). Поддерживает `--dry-run`, `--skip-install`,
  `--branch` (проверка, что локально выбрана ожидаемая ветка). Комбинируется
  с bootstrap-ubuntu.sh: `./deploy/deploy.sh user@host --skip-install &&
  ssh user@host 'cd ttx-src && sudo ./deploy/bootstrap-ubuntu.sh ...'`
  для полностью удалённого разворачивания в одну связку команд.
- **Docker:** скопировать все `*.example` в файлы без `.example`, заменить
  домен/пароли и положить сертификаты в `compose/certs/`; сначала поднять
  только `x-ui`, создать в панели API token, вписать его в `bridge.json`, затем
  выполнить `docker compose up -d --build`. Полная последовательность есть в README.

## 12. Дальнейшее развитие (не входит в текущую поставку)

Из README §6, плюс технический долг, выявленный при реорганизации:

- Политики по группам: несколько ingress-inbound'ов + несколько экземпляров
  endpoint'а через `trusttunnel@.service`, раздельная статистика.
- Двусторонняя синхронизация пользователей `credentials.toml` ↔ клиенты панели.
- Общий порт 443 через SNI-fallback вместо выделенного порта под TrustTunnel.
- Docker: file-watcher entrypoint или sidecar для перезапуска
  `trusttunnel`-контейнера при смене `vpn.toml` вместо ручного
  `docker compose restart` (сейчас `restart_on_change=false` — ручная операция).
- `deploy/deploy.sh` не автоматизирует первичное заполнение секретов
  (`bridge.json`, `vpn.base.toml`, сертификаты) на новом сервере — сознательно,
  чтобы не гонять пароли/ключи по недоверенным каналам без явного шага
  оператора; если появится безопасный способ (например, интеграция с
  секрет-менеджером), стоит добавить отдельный `deploy/push-secrets.sh`.

## 13. Критерии приёмки

Локальная приёмка (`./tests/check.sh`):

1. Все shell-скрипты проходят `bash -n`.
2. Все JSON-шаблоны проходят стандартный JSON-парсер Python.
3. `ttx_bridge.py` компилируется и unit-тесты подтверждают идемпотентный
   рендер, отказ от захвата чужого inbound и защиту от петли портов.
4. При наличии Docker выполняется `docker compose config`.

Серверная приёмка (`sudo /opt/ttx/tests/e2e-smoke.sh`):

1. Xray слушает настроенный ingress-порт.
2. HTTP-запрос через SOCKS5 ingress получает внешний IP.
3. Итоговый `vpn.toml` содержит управляемый SOCKS5 upstream.
4. Сервисы `x-ui` и `trusttunnel` активны.
5. UDP/QUIC проверяется отдельно с клиента, поскольку локальный SOCKS-тест
   не доказывает весь путь UDP ASSOCIATE через TrustTunnel.

Полный e2e требует Linux-сервера, домена/сертификата и реального клиента;
локальная macOS-проверка не подменяет этот этап.

## 14. Журнал изменений

- **2026-08-16** — завершён аудит поставки: добавлены Bearer API token и CSRF
  для актуального API 3x-ui, безопасное владение inbound только по `remark`,
  сохранение счётчиков при update; исправлены официальные ключи TrustTunnel
  (`metrics.address`, отдельный `hosts.toml`); Docker Compose переведён на
  штатный `/trusttunnel_endpoint`, добавлены Dockerfile bridge и примеры
  `credentials/hosts/rules`; bootstrap читает сгенерированный API token 3x-ui;
  добавлены unit-тесты и единый `tests/check.sh`.

- **2026-08-14** — добавлен `deploy/bootstrap-ubuntu.sh`: полное
  автоматическое развёртывание на чистом Ubuntu-сервере от `apt-get update`
  до включённых systemd-сервисов и e2e-smoke-теста в одну команду. Отдельно
  автоматизирована установка совместимого Python без вмешательства в
  системный `/usr/bin/python3`.
  Единственный сознательно неавтоматизированный шаг — интерактивный
  `setup_wizard` TrustTunnel (см. §11). Также создан приватный репозиторий
  `avarabey/CascadeVPN` на GitHub и запушена вся история.
- **2026-08-13** — репозиторий разложен по каталогам (`bridge/`, `install/`,
  `templates/`, `systemd/`, `tests/`, `compose/`, `deploy/`) в соответствии
  со структурой, уже подразумевавшейся в `install/ttx-install.sh` и README;
  добавлены отсутствовавшие файлы: `bridge/bridge.example.json`,
  `templates/compat.json`, `templates/vpn.base.toml.example`, все юниты
  `systemd/`, `compose/.env.example`, `compose/bridge.docker.json`,
  `compose/vpn.base.toml.example`, `.gitignore`; добавлен
  `deploy/deploy.sh` для удалённого развёртывания по SSH; создан этот файл.
