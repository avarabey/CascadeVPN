# SPEC — CascadeVPN / ffknd portal

**Версия спецификации:** 0.2.1

**Версия REPO:** [0.2.1](REPO_VERSION.md)

**Состояние:** production cutover публичного `443/tcp` завершён; portal 0.1.1
с исправленным QR-rendering развёрнут, критерии §14.3 выполнены 2026-08-20.

Живой документ. Обновляется по ходу работы над проектом; в конце — журнал
изменений. Дублирует часть [README.md](README.md) и [ARCHITECTURE.md](ARCHITECTURE.md),
но собирает всё в одном месте и добавляет то, что в них не поместилось:
точную схему конфигурации, карту репозитория, эксплуатационные детали.

## 1. Назначение и рамки проекта

**Задача:** дать трафику TrustTunnel (endpoint, маскирующийся под HTTPS)
попасть под управление панели 3x-ui (Xray) — маршрутизация, гео-правила,
outbound'ы (direct/WARP/VLESS-цепочки/blackhole) — без форка и без патчинга
исходников обоих upstream-проектов. Для обычного браузерного трафика на том же
домене проект также предоставляет нейтральный личный портал ffknd.

**Не является целью проекта:**
- Замена или обёртка функциональности 3x-ui/TrustTunnel по отдельности —
  оба ставятся и работают их штатными средствами.
- Пофамильный биллинг/квоты на уровне ingress'а (см. §10, «Учёт трафика» —
  known limitation, а не фича в разработке).
- Поддержка версий 3x-ui/TrustTunnel вне матрицы `templates/compat.json`.

Runtime-код проекта состоит из двух изолированных частей на стандартной
библиотеке Python 3.11+, без внешних Python-зависимостей:

- `bridge/ttx_bridge.py` — демон-медиатор между TrustTunnel и 3x-ui;
- `portal/app/` — HTTP-приложение ffknd со статическим frontend и SQLite.

Портал не импортирует мост и не участвует в обработке VPN CONNECT. Их связывает
только конфигурация TrustTunnel и сценарии развёртывания.

## 2. Архитектура production ffknd.ru

```
Internet :443/tcp
        │
        ▼
Nginx stream + ssl_preread
        ├── SNI ffknd.ru / www.ffknd.ru
        │      └──► 127.0.0.1:9443  Nginx TLS
        │                └──► 127.0.0.1:8080  ffknd portal
        │
        └── любой другой или пустой SNI
               └──► 127.0.0.1:10443  Xray VLESS Reality

TrustTunnel независимо слушает :8443/tcp + :8443/udp
        └──► 127.0.0.1:10800  Xray SOCKS ingress / routing
```

Production-сервер не использует TrustTunnel как владельца публичного
`443/tcp`: этот порт до cutover занят Xray Reality, после cutover — только
Nginx stream. Nginx читает SNI, но не завершает TLS на Reality-ветке и не
добавляет PROXY protocol, поэтому Xray получает исходный ClientHello.

Reality-клиенты используют SNI из набора `cloud.ru*`; значения
`ffknd.ru`/`www.ffknd.ru` для них запрещены, иначе запрос попадёт в портал.
TrustTunnel остаётся отдельным сервисом на `8443/tcp+udp`; его конфигурация и
порт при cutover не меняются.

Старая схема, где TrustTunnel единолично владеет `443/tcp+udp` и
`[reverse_proxy]` делит HTTP path с CONNECT, остаётся reference/Compose-моделью
и не должна применяться installer'ом к текущему production-серверу.

## 3. Карта репозитория

```
CascadeVPN/
├── REPO_VERSION.md            — версия репозитория и ссылка на эту спецификацию
├── HANDOFF.md                 — безопасная точка продолжения для другого агента
├── README.md                 — обзор, установка, эксплуатация (для оператора)
├── ARCHITECTURE.md           — ADR: почему так, а не иначе
├── RUNBOOK.md                — диагностика инцидентов
├── SPEC.md                   — этот файл: детальная спецификация
├── bridge/
│   ├── ttx_bridge.py         — демон-мост (CLI: reconcile|watch|status|doctor)
│   ├── bridge.example.json   — образец конфига моста (→ /etc/ttx/bridge.json)
│   └── Dockerfile            — минимальный образ bridge на Python 3.12
├── portal/
│   ├── app/                   — stdlib HTTP/API, SQLite и статический frontend
│   ├── configure_reverse_proxy.py — безопасно добавляет origin в vpn.base.toml
│   ├── content/reference/     — памятки Git/Linux/Docker/HTTP/Regex в Markdown
│   ├── migrations/            — миграции SQLite
│   ├── tests/                 — unit/API/security-тесты портала
│   ├── Dockerfile             — образ Python 3.12 без внешних пакетов
│   └── portal.env.example     — пример переменных среды
├── install/
│   └── ttx-install.sh        — ставит 3x-ui + TrustTunnel + обвязку (bare-metal)
├── templates/
│   ├── compat.json           — матрица min/tested версий upstream (→ /etc/ttx/compat.json)
│   └── vpn.base.toml.example — образец базового конфига TrustTunnel
├── systemd/
│   ├── trusttunnel.service.d/10-ttx-overlay.conf  — drop-in к юниту TrustTunnel
│   ├── ffknd-portal.service    — портал на loopback:8080 от ttx-portal
│   ├── ttx-bridge.service     — постоянный демон (`ttx watch`), основной режим
│   ├── ttx-reconcile.service  — разовый reconcile
│   └── ttx-reconcile.timer    — таймер-альтернатива watch-режиму
├── tests/
│   ├── check.sh               — локальная проверка синтаксиса, JSON и unit-тестов
│   ├── test_bridge.py         — unit-тесты рендера, ownership и защиты от петель
│   ├── e2e-smoke.sh           — сквозная проверка на установленном сервере
│   ├── port443-smoke.sh       — smoke reference-схемы TrustTunnel на 443
│   ├── reality-e2e-smoke.sh   — реальный VLESS Reality smoke без вывода секретов
│   └── reality_smoke_config.py — root-only генератор временного client config
├── nginx/
│   ├── modules-enabled/70-ffknd-stream.conf — верхнеуровневый stream context
│   ├── stream-conf.d/ffknd-router.conf      — SNI router 443
│   ├── sites-available/                     — ACME:80 и loopback TLS:9443
│   └── renewal-hooks/deploy/                — validate + reload после renew
├── compose/
│   ├── docker-compose.yml     — x-ui + trusttunnel + bridge + portal
│   ├── .env.example           — версии, панель и настройки portal
│   ├── bridge.docker.json     — образец bridge.json под docker-сеть
│   ├── vpn.base.toml.example  — образец базового конфига под контейнер (порт 8443)
│   ├── credentials.toml.example — пример пользователей TrustTunnel
│   ├── hosts.toml.example       — пример TLS-хоста TrustTunnel
│   └── rules.toml.example       — пример файла правил TrustTunnel
└── deploy/
    ├── deploy.sh               — generic/reference rsync+installer; не для live cutover
    ├── xui_portal_cutover.py   — API-транзакция inbound 6 + backup/rollback
    ├── live_portal_cutover.sh  — one-shot production cutover с аварийным rollback
    ├── live_xui_probe.py       — redacted read-only probe 3x-ui
    └── bootstrap-ubuntu.sh     — полное автоматическое развёртывание НА сервере
                                  (Ubuntu): apt-зависимости → install/ttx-install.sh →
                                  bridge.json → doctor/reconcile → systemd enable → smoke-test
```

Все пути внутри `install/ttx-install.sh` (`$SRC/bridge/...`,
`$SRC/templates/...`, `$SRC/systemd/...`, `$SRC/tests/...`) рассчитаны
именно на эту раскладку: `SRC` вычисляется как родитель каталога `install/`.

## 4. ffknd portal

Портал — самостоятельное приложение на стандартной библиотеке Python 3.11+
и SQLite. Он раздаёт статический HTML/CSS/JavaScript и JSON API через
`ThreadingHTTPServer`; внешних Python и frontend-зависимостей нет. При старте
применяются SQL-миграции, SQLite переводится в WAL-режим, затем запускаются
фоновые циклы мониторинга и обновления лент.

### 4.1. Функции и место хранения

| Функция | Выполнение и хранение |
|---|---|
| Статус сервисов | Сервер проверяет HTTP(S), сохраняет код, задержку и ошибку в SQLite |
| Веб-инструменты | JSON, Base64, UUID v4, Unix timestamp, URL encode/decode и пароли работают только в браузере |
| Зашифрованный блокнот | Web Crypto: PBKDF2-SHA256 → AES-256-GCM; только шифротекст в `localStorage` |
| RSS/Atom | Сервер получает и разбирает ленты, хранит источники и последние элементы в SQLite |
| Короткие ссылки | Код и target в SQLite; публичный `GET /s/<code>` возвращает 302 и считает переходы |
| QR-коды | SVG генерируется локально в браузере, без внешнего API; QR-контейнер сбрасывает общую icon-stroke стилизацию |
| Технический справочник | Markdown из `portal/content/reference/`: Git, Linux, Docker, HTTP, Regex |

Мастер-пароль блокнота не связан с паролем входа в портал. Он, производный
ключ и открытый текст заметок не отправляются серверу. Очистка данных сайта
удаляет единственную локальную копию; восстановление забытого мастер-пароля не
предусмотрено.

### 4.2. Публичная и личная границы

Без сессии доступны главная статика, health check, агрегированный статус
сервисов без URL/текста ошибок, элементы RSS-ленты, справочник и переход по уже
созданной короткой ссылке. Сессия требуется для добавления и удаления сервисов,
ручного запуска проверок, управления RSS-источниками, обновления лент и CRUD
коротких ссылок.

Пароль входа не хранится: оператор задаёт PBKDF2-SHA256-хеш через
`PORTAL_PASSWORD_HASH`. Случайный session token уходит в
HttpOnly/Secure/SameSite=Strict cookie, в SQLite хранится только SHA-256-хеш
токена. Изменяющие запросы дополнительно требуют CSRF-токен и совпадающий
Origin. Восемь неудачных входов с одного адреса за пять минут временно
блокируют следующие попытки.

### 4.3. Переменные среды

Основной пример — `portal/portal.env.example`; bare-metal читает
`/etc/ttx/portal.env`, Compose — `compose/.env` и секцию `environment`.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `PORTAL_PASSWORD_HASH` | нет, обязательна | результат `python3 -m app hash-password`; пустое значение останавливает запуск |
| `PORTAL_HOST` | `127.0.0.1` | адрес origin; не меняйте на публичный без отдельной модели угроз |
| `PORTAL_PORT` | `8080` | внутренний порт origin |
| `PORTAL_DB` | `/data/portal.db` | SQLite; bare-metal env и systemd unit задают `/var/lib/ffknd-portal/portal.db` |
| `PORTAL_PUBLIC_URL` | `https://localhost` | внешний origin и база для коротких URL |
| `PORTAL_SESSION_TTL` | `43200` | срок сессии, секунды |
| `PORTAL_COOKIE_SECURE` | `true` | добавлять Secure к session cookie |
| `PORTAL_TRUST_PROXY` | `false` | доверять первому `X-Forwarded-For`; для штатной схемы не требуется |
| `PORTAL_ALLOW_PRIVATE_URLS` | `false` | разрешить RSS/monitoring к private/loopback-адресам |
| `PORTAL_ALLOWED_OUTBOUND_PORTS` | `80,443` | допустимые исходящие HTTP(S)-порты |
| `PORTAL_CHECK_TIMEOUT` | `5` | timeout проверки сервиса, секунды |
| `PORTAL_FEED_TIMEOUT` | `10` | timeout загрузки ленты, секунды |
| `PORTAL_MONITOR_INTERVAL` | `60` | период фоновой проверки; `0` отключает цикл |
| `PORTAL_FEED_REFRESH_INTERVAL` | `900` | период обновления RSS; `0` отключает цикл |

Исходящие URL нормализуются и проверяются до соединения. По умолчанию
разрешены только глобальные IP и порты 80/443; redirect проходит ту же
проверку. Соединение закрепляется за уже проверенным DNS-адресом, что снижает
риск DNS rebinding. Link-local и metadata-адреса запрещены даже при
`PORTAL_ALLOW_PRIVATE_URLS=true`.

### 4.4. Контракт production-портов

| Endpoint | Владелец после cutover | Назначение |
|---|---|---|
| `0.0.0.0:443/tcp` | Nginx stream | SNI demux: portal или Xray Reality |
| `127.0.0.1:9443/tcp` | Nginx HTTP/TLS | TLS termination портала |
| `127.0.0.1:8080/tcp` | ffknd portal | HTTP origin, наружу не публикуется |
| `127.0.0.1:10443/tcp` | Xray | VLESS Reality backend, наружу не публикуется |
| `0.0.0.0:8443/tcp+udp` | TrustTunnel | независимый VPN endpoint |
| `127.0.0.1:10800` | Xray | SOCKS ingress для TrustTunnel chain |

`nginx/stream-conf.d/ffknd-router.conf` содержит две точные web-ветки:
`ffknd.ru` и `www.ffknd.ru` → `127.0.0.1:9443`. `default` всегда указывает
на `127.0.0.1:10443`. Публичный TLS-сертификат завершается только на web-ветке.
Certbot использует webroot `/var/www/letsencrypt`; deploy hook сначала
выполняет `nginx -t` и только затем reload.

Inbound 3x-ui `id=6` хранится постоянно как `listen=127.0.0.1`, `port=10443`,
но managed Host и `externalProxy` рекламируют клиентам `ffknd.ru:443` с
Reality SNI `cloud.ru`. `settings`, Reality target, private key, short IDs и
клиенты при переносе не меняются. Применение выполняется только через
`deploy/xui_portal_cutover.py`, который использует loopback Bearer API,
создаёт online SQLite backup и полный root-only rollback state.

## 5. Конфигурация моста — `bridge.json`

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
| `trusttunnel.restart_on_change` | bool | `true` | `false` в Docker — оператор рестартует контейнер снаружи |
| `trusttunnel.command_timeout_secs` | number | `30` | timeout каждого `systemctl restart`/`is-active`; должен быть больше 0 |
| `compat.file` | path | `/etc/ttx/compat.json` | матрица совместимости |
| `compat.strict` | bool | `false` | `true` = отказ работать при несовпадении версии |
| `interval_secs` | int | `60` | период цикла в режиме `watch` |

Неизвестные ключи верхнего уровня (например, `_comment`) игнорируются —
это осознанное решение (AD-4, п.4), чтобы будущие поля не ломали парсинг.

## 6. Алгоритм `reconcile`

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
6. `validate_vpn_config` — TOML-валидация до любой записи; apply lock сериализует
   одновременные watch/timer/manual запуски.
7. Состояние target непосредственно перед apply фиксируется в памяти и
   `<file>.ttx-bak`; target пишется через `fsync` + `os.replace` с сохранением
   метаданных. Если контент не изменился, ничего не пишется и не перезапускается.
8. При `restart_on_change=true` мост выполняет `systemctl restart`, затем
   `systemctl is-active`, оба с timeout `command_timeout_secs`. Ошибка любого шага
   атомарно возвращает состояние до apply, повторно запускает прежнюю конфигурацию
   и завершает `reconcile` кодом 4. В Docker (`restart_on_change=false`) мост не может
   выполнить внешний restart/health check; его делает оператор Compose.

Идемпотентность: повторный прогон над уже управляемым файлом даёт тот же
результат (маркер и секция не дублируются, см. `AD-5`).

## 7. CLI

```
ttx reconcile [--dry-run] [-v]   # один цикл согласования; --dry-run печатает
                                   бы-записанный конфиг, ничего не меняя
ttx reconcile --no-restart       # записать без systemctl restart; Docker/manual apply
ttx watch     [--dry-run] [-v]   # цикл reconcile каждые interval_secs (сервис по умолчанию)
ttx status                        # текущее состояние ingress + куда указывает vpn.toml
ttx doctor                        # предполётные проверки (см. ниже)
```

`ttx doctor` проверяет: существование `vpn.base.toml`, успешный логин в
панель, `allow_private_network_connections != true` в базовом конфиге.

## 8. Два режима работы демона

- **`ttx-bridge.service`** (по умолчанию, включается в README) — долгоживущий
  процесс, сам крутит цикл `watch` с интервалом `interval_secs`.
- **`ttx-reconcile.timer` + `ttx-reconcile.service`** — альтернатива на
  systemd-таймере (`OnUnitActiveSec=60s`) для тех, кто предпочитает
  штатный планировщик встроенному циклу. **Не включайте оба одновременно**
  на одном сервере — это лишняя нагрузка на API панели, без функциональной
  пользы.
- **`trusttunnel.service.d/10-ttx-overlay.conf`** — не альтернатива, а
  дополнение к любому из двух режимов: оно задаёт только порядок запуска.
  `ExecStartPre` намеренно нет: изменение `vpn.toml` внутри start-транзакции юнита
  нельзя надёжно проверить и откатить. Bootstrap делает первичный `reconcile`
  до `enable --now`, а дальше конфиг применяет ttx-bridge с health check и rollback.
  На production старый drop-in с `ExecStartPre` заменён этой версией без
  перезапуска endpoint; предыдущий файл сохранён как
  `/etc/systemd/system/trusttunnel.service.d/10-ttx-overlay.conf.before-portal-v0.2.0`.

## 9. Docker-вариант — отличия от bare-metal

- `x-ui` и `trusttunnel` — разные контейнеры в общей сети `ttx`; `bridge`
  использует `panel.base_url=http://xui:2053` (DNS-алиас), а
  `trusttunnel.reach_host=xui`, чтобы контейнер TrustTunnel мог достучаться
  до ingress-inbound по сети docker, а не по `127.0.0.1`.
- `ingress.listen=0.0.0.0` внутри контейнера x-ui (иначе Xray слушал бы
  loopback, недоступный извне контейнера).
- Общий named volume монтируется в штатный upstream-путь
  `/trusttunnel_endpoint`; итоговый `vpn.toml` пишет bridge, а
  `credentials.toml`, `hosts.toml` и `rules.toml` подключаются read-only.
- `portal` запускается от непривилегированного пользователя `portal`, монтирует
  отдельный `portal-data` для SQLite и использует
  `network_mode: "service:trusttunnel"`. Он делит с endpoint network namespace
  и слушает их общий `127.0.0.1:8080`; секции `ports` у portal нет.
  Обычный restart TrustTunnel сохраняет namespace; при пересоздании его
  контейнера нужно пересоздать и portal.
- `[reverse_proxy]` в `compose/vpn.base.toml` указывает на
  `127.0.0.1:8080`. Наружу публикуются только `443:8443/tcp` и
  `443:8443/udp` сервиса TrustTunnel; portal не может конкурировать за них.
- `trusttunnel.restart_on_change=false` — `systemctl` внутри контейнера
  недоступен; перезапуск контейнера при изменении `vpn.toml` — ручная
  операция (`docker compose restart trusttunnel`) либо задача на будущее
  (entrypoint с file-watcher, см. §13).
- Порты хоста: `443/tcp+udp → 8443` контейнера; `vpn.base.toml.example`
  для compose отражает это (`listen_address = "0.0.0.0:8443"`).

## 10. Безопасность и известные ограничения

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
  `rules.toml`, `certs/`, `portal.env` и значение
  `PORTAL_PASSWORD_HASH` не должны попадать
  в git — исключены в `.gitignore`; `deploy/deploy.sh` явно исключает их
  из синхронизации на сервер, чтобы не затирать то, что уже настроено на месте.
  На bare-metal `credentials.toml` и `vpn.toml` обязаны иметь mode `0600`;
  endpoint запускается от root и в более широких правах не нуждается.
- **Portal SQLite**: bare-metal база и WAL лежат в
  `/var/lib/ffknd-portal`, Docker — в volume `portal-data`; каталог доступен
  только сервисному пользователю. Резервная копия SQLite не включает заметки,
  потому что они остаются в `localStorage` браузера.
- **Portal SSRF**: RSS и мониторинг по умолчанию блокируют private, loopback,
  link-local и нестандартные порты. `PORTAL_ALLOW_PRIVATE_URLS=true` расширяет
  доступ к внутренней сети и требует отдельного решения оператора.
- **Portal auth**: пустой `PORTAL_PASSWORD_HASH` блокирует запуск. Cookie
  Secure/SameSite, CSRF и Origin-проверка предполагают корректный
  `PORTAL_PUBLIC_URL` и внешний HTTPS.
- **Docker restart_on_change**: см. §9 — при отключённом рестарте изменения
  `vpn.toml` не подхватятся сами, пока кто-то не перезапустит контейнер.

## 11. Совместимость версий (см. также ARCHITECTURE.md AD-4)

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
  — `forward_protocol.socks5`, `reverse_proxy`, структура `vpn.toml`, metrics
  и отдельные `hosts.toml`/`credentials.toml`/`rules.toml`.
- [TrustTunnel Dockerfile.prebuilt](https://github.com/TrustTunnel/TrustTunnel/blob/master/Dockerfile.prebuilt)
  и [docker-entrypoint.sh](https://github.com/TrustTunnel/TrustTunnel/blob/master/docker-entrypoint.sh)
  — рабочий каталог `/trusttunnel_endpoint` и обязательные файлы контейнера.
- [3x-ui API authentication](https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/authentication.mdx)
  — Bearer token и cookie-сессия с CSRF.
- [3x-ui inbounds API](https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/inbounds.mdx)
  — list/add/update endpoints и формат полей inbound.

## 12. Развёртывание

### 12.1. Bare-metal / Ubuntu

> **Reference/new-host workflow only.** На текущем production-хосте
> `ffknd.ru` этот bootstrap/installer запускать нельзя: там уже существуют
> Xray Reality на `443` и TrustTunnel на `8443`, а production cutover описан
> отдельно в §12.4.

`sudo ./deploy/bootstrap-ubuntu.sh` ставит системные зависимости, upstream,
bridge и portal, раскладывает systemd-юниты, формирует `bridge.json`, выполняет
`doctor`/`reconcile` и серверный e2e-smoke. Если системный Python ниже 3.11,
bootstrap пытается установить отдельный `python3.11`, не заменяя
`/usr/bin/python3`, и указывает его в CLI и portal unit. Опциональный
`--configure-firewall` открывает только `443/tcp+udp` после сохранения
доступа к текущему SSH-порту.

Скрипт сознательно не автоматизирует интерактивный `setup_wizard`
TrustTunnel и не придумывает пароль портала. Если
`/etc/ttx/portal.env` отсутствует или `PORTAL_PASSWORD_HASH` пуст/неверного формата,
bootstrap не включает и при необходимости останавливает `ffknd-portal`. Настройка после
установки:

```bash
cd /opt/ttx/portal
python3 -m app hash-password
sudo cp -n /etc/ttx/portal.env.example /etc/ttx/portal.env
sudo chmod 0600 /etc/ttx/portal.env
sudoedit /etc/ttx/portal.env
# PORTAL_PASSWORD_HASH=<полный выведенный хеш>
# PORTAL_PUBLIC_URL=https://ffknd.ru
sudo systemctl enable --now ffknd-portal
```

`trusttunnel.service` не имеет зависимостей `Wants=` или `Requires=` на `ffknd-portal.service`:
туннель запускается и при намеренно выключенном портале.

`install/ttx-install.sh` копирует portal в `/opt/ttx/portal`, создаёт
`ttx-portal`, каталог `/var/lib/ffknd-portal` с режимом `0700` и unit. Если
`/etc/ttx/vpn.base.toml` уже существует и не содержит `[reverse_proxy]`,
установщик атомарно добавляет loopback-origin между своими маркерами и
сохраняет точечный snapshot `vpn.base.toml.portal-bak`. Существующую таблицу
`[reverse_proxy]` он не захватывает и не перезаписывает. Штатный rollback
`ffknd-portal-config remove /etc/ttx/vpn.base.toml` удаляет только байт-точный
управляемый блок и сохраняет более поздние правки оператора.

При ручной последовательности, где `setup_wizard` и копирование
`vpn.base.toml` выполняются после установщика, оператор должен добавить блок
из §4.4 сам, затем выполнить:

```bash
sudo ttx doctor
sudo ttx reconcile --dry-run
sudo ttx reconcile
sudo systemctl enable --now x-ui ffknd-portal trusttunnel ttx-bridge
```

Удалённый `deploy/deploy.sh user@host` синхронизирует репозиторий и запускает
installer по SSH, но не копирует секреты. Он поддерживает `--dry-run`,
`--skip-install` и `--branch`; `portal.env`, сертификаты и остальные секреты
оператор заполняет на сервере.

### 12.2. Docker Compose

Подготовка выполняется до общего `up`:

```bash
cd compose
cp .env.example .env
cp bridge.docker.json bridge.json
cp vpn.base.toml.example vpn.base.toml
cp credentials.toml.example credentials.toml
cp hosts.toml.example hosts.toml
cp rules.toml.example rules.toml
mkdir -p certs

cd ../portal
python3 -m app hash-password
cd ../compose
# вставить хеш как PORTAL_PASSWORD_HASH=... и проверить PORTAL_PUBLIC_URL
```

После сертификатов и TrustTunnel-файлов сначала запускается `x-ui`; оператор
меняет начальные реквизиты, создаёт API token через SSH-туннель к панели и
вписывает его в `bridge.json`. Затем:

```bash
docker compose up -d x-ui
# настроить panel token и bridge.json
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 portal trusttunnel bridge
```

Compose не публикует `8080`: portal делит namespace с TrustTunnel и доступен
endpoint только на `127.0.0.1:8080`. Пустой `PORTAL_PASSWORD_HASH` приводит к
fail-closed запуску portal, а не к порталу без авторизации.

### 12.3. Проверка и откат

После развёртывания локальный `e2e-smoke.sh` проверяет SOCKS5-цепочку, но не
доказывает разделение публичного `443`. С внешней машины нужно отдельно
запустить `tests/port443-smoke.sh` с `PORTAL_PUBLIC_URL`,
`TT_CLIENT_USERNAME`, `TT_CLIENT_PASSWORD`; режим `PORT443_WEB_ONLY=1`
проверяет только сайт и недостаточен для VPN-приёмки.

Аварийная остановка только portal (`systemctl disable --now ffknd-portal` или
`docker compose stop portal`) не меняет `[forward_protocol.socks5]` и должна
оставить VPN CONNECT рабочим. Полный откат reverse proxy в bare-metal
выполняется хирургически через `ffknd-portal-config remove`, а не заменой
текущего файла старым `vpn.base.toml.portal-bak`. В Docker оператор удаляет
свою секцию из `compose/vpn.base.toml`; затем нужны
`ttx reconcile --dry-run`/`ttx reconcile` или перезапуск bridge и TrustTunnel
соответственно. Пошаговый сценарий и проверки
описаны в [RUNBOOK.md](RUNBOOK.md#откат-только-портала).

### 12.4. Production cutover ffknd.ru

Предусловия:

1. `ffknd-portal` отвечает на `127.0.0.1:8080`.
2. Nginx TLS endpoint отвечает с доверенным сертификатом на
   `127.0.0.1:9443`; сертификат содержит `ffknd.ru` и `www.ffknd.ru`.
3. Xray inbound `id=6` local (`nodeId=null`), VLESS Reality, target
   `cloud.ru:443`; его `serverNames` не содержат домены портала.
4. `10443` свободен; 8080/9443/10443 не доступны снаружи.
5. Сохранены online backup БД 3x-ui, generated Xray config и Nginx/UFW config.
6. `deploy/xui_portal_cutover.py dry-run` успешен, а его SHA совпадает с
   версией, зафиксированной в [HANDOFF.md](HANDOFF.md).

`deploy/live_portal_cutover.sh` выполняет один контролируемый переход:

1. Через loopback Bearer API 3x-ui переносит inbound 6 на
   `127.0.0.1:10443`; managed Host и `externalProxy` продолжают рекламировать
   `ffknd.ru:443` с SNI `cloud.ru`.
2. Ждёт Xray listener на 10443 и освобождение публичного 443.
3. Устанавливает production stream-конфигурацию, выполняет `nginx -t` и
   graceful reload.
4. Проверяет trusted portal TLS и default SNI passthrough к Reality target.

Любая ошибка вызывает аварийный rollback: Nginx stream немедленно
останавливается, исходный inbound payload восстанавливается через API,
Xray снова занимает публичный 443, а Nginx запускается без stream wrapper.
Активные Reality-соединения во время cutover/rollback могут кратко оборваться.

После успеха обязательно выполнить `tests/reality-e2e-smoke.sh` с фактическим
`appliedTag` из root-only cutover state и повторить проверку после явного
restart Xray. Только это доказывает, что перенос сохранён в 3x-ui и обычный
клиентский профиль продолжает работать.

## 13. Дальнейшее развитие (не входит в текущую поставку)

Из README §6, плюс технический долг, выявленный при реорганизации:

- Политики по группам: несколько ingress-inbound'ов + несколько экземпляров
  endpoint'а через `trusttunnel@.service`, раздельная статистика.
- Двусторонняя синхронизация пользователей `credentials.toml` ↔ клиенты панели.
- Автоматический backup/export SQLite портала и переносимая выгрузка
  зашифрованного блокнота из браузера.
- Более детальная история uptime и уведомления о сбоях сервисов.
- Docker: file-watcher entrypoint или sidecar для перезапуска
  `trusttunnel`-контейнера при смене `vpn.toml` вместо ручного
  `docker compose restart` (сейчас `restart_on_change=false` — ручная операция).
- `deploy/deploy.sh` не автоматизирует первичное заполнение секретов
  (`bridge.json`, `vpn.base.toml`, `portal.env`, сертификаты) на новом сервере — сознательно,
  чтобы не гонять пароли/ключи по недоверенным каналам без явного шага
  оператора; если появится безопасный способ (например, интеграция с
  секрет-менеджером), стоит добавить отдельный `deploy/push-secrets.sh`.

## 14. Критерии приёмки

### 14.1. Локальная приёмка

Локальная приёмка (`./tests/check.sh`):

1. Все shell-скрипты проходят `bash -n`.
2. Все JSON-шаблоны проходят стандартный JSON-парсер Python.
3. `ttx_bridge.py` компилируется и unit-тесты подтверждают идемпотентный
   рендер, отказ от захвата чужого inbound и защиту от петли портов.
4. Python-модули portal компилируются; его unit/API-тесты проверяют auth,
   SQLite, RSS, outbound-policy и основные маршруты.
5. Публичная статика не содержит внутренних имён TrustTunnel/3x-ui/Xray;
   при наличии Node каждый frontend-модуль проходит `node --check`.
6. Контракт разделения HTTP path и authority-form CONNECT сверяется с
   зафиксированным fixture TrustTunnel; при наличии Docker выполняется
   `docker compose config`.

### 14.2. Reference TrustTunnel-приёмка

Серверная приёмка (`sudo /opt/ttx/tests/e2e-smoke.sh`):

1. Xray слушает настроенный ingress-порт.
2. HTTP-запрос через SOCKS5 ingress получает внешний IP.
3. Итоговый `vpn.toml` содержит управляемый SOCKS5 upstream.
4. Сервисы `x-ui` и `trusttunnel` активны.
5. UDP/QUIC проверяется отдельно с клиента, поскольку локальный SOCKS-тест
   не доказывает весь путь UDP ASSOCIATE через TrustTunnel.

Дополнительно, если portal включён, вручную проверьте, что
`http://127.0.0.1:8080/api/health` возвращает `status=ok`, а итоговый
`vpn.toml` содержит `[reverse_proxy]` на этот origin.

Внешняя приёмка общего порта (`./tests/port443-smoke.sh`) требует:

```bash
PORTAL_PUBLIC_URL=https://ffknd.ru
TT_CLIENT_USERNAME=<пользователь TrustTunnel>
TT_CLIENT_PASSWORD=<пароль TrustTunnel>
```

Она подтверждает обычный HTTPS GET, health endpoint и VPN CONNECT через один
публичный `443`. `PORT443_WEB_ONLY=1` разрешён только как предварительная
проверка сайта.

Полный e2e требует Linux-сервера, домена/сертификата и реального клиента;
локальная macOS-проверка не подменяет этот этап.

### 14.3. Production-приёмка ffknd.ru

Версия `0.2.0` выпускается только если одновременно выполнено всё ниже:

1. `https://ffknd.ru/api/health` с внешней машины возвращает `status=ok`
   без `-k`; сертификат и hostname валидны.
2. `https://www.ffknd.ru/...` делает фиксированный 308 на apex-домен.
3. Nginx — единственный listener публичного `443/tcp`; portal слушает только
   `127.0.0.1:8080`, TLS terminator — только `127.0.0.1:9443`, Xray Reality —
   только `127.0.0.1:10443`.
4. `tests/reality-e2e-smoke.sh` устанавливает настоящий VLESS Reality tunnel
   через `ffknd.ru:443` и получает HTTPS-ответ через SOCKS, не выводя ключи.
5. `trusttunnel` остаётся active на `8443/tcp+udp`; его конфиг не изменён.
6. `x-ui`, `nginx`, `ffknd-portal`, `trusttunnel` active, в их свежих логах
   нет ошибок bind/restart/upstream.
7. После явного restart Xray пункты 1, 3 и 4 всё ещё проходят.
8. Root-only rollback state и online backup существуют и проходят
   SQLite `quick_check`.

**Результат 2026-08-20: принято.** Внешний health endpoint вернул
`{"status":"ok","version":"0.1.1"}` без отключения TLS-проверки; `www`
вернул фиксированный `308`; listener-карта совпала с §4.4. Реальный
VLESS Reality smoke прошёл до и после явного restart Xray, а проверка
default SNI получила валидный сертификат `cloud.ru`. Сервисы `x-ui`, `nginx`,
`ffknd-portal`, `trusttunnel` и `ttx-bridge` остались active; online backup
`/var/lib/ffknd-xui-cutover/x-ui-before-20260820T221038Z.db` прошёл
`PRAGMA quick_check`.

При аудите был обнаружен один краткий неуспешный старт TrustTunnel во время
рестарта x-ui: старый live drop-in запускал `ttx reconcile` в `ExecStartPre`,
когда loopback API панели ещё не отвечал. Systemd автоматически восстановил
сервис через три секунды. Устаревший `ExecStartPre` удалён, `daemon-reload`
выполнен без перезапуска VPN; после исправления сервисы, listeners, portal TLS
и реальный Reality smoke повторно проверены.

### 14.4. Защита production-хоста от исчерпания памяти

Production VM имеет 1 GiB RAM. Системные VPN-сервисы живут в
`system.slice`, а интерактивные root/SSH-сессии — в `user-0.slice`.
После инцидента 2026-08-21 последняя получает отдельный лимит:

- `MemoryHigh=384M` включает reclaim до глобального дефицита;
- `MemoryMax=512M` не даёт отсоединённым root-процессам забрать всю RAM;
- `MemorySwapMax=512M` ограничивает swap этой slice;
- `TasksMax=256` ограничивает дерево процессов/потоков SSH-сессий;
- root-only swapfile `/var/lib/ffknd-memory/swapfile` размером 1 GiB
  с `vm.swappiness=10` даёт ядру короткий аварийный запас, но не
  делает swap обычным режимом работы.

`deploy/harden-memory.sh` реализует `dry-run`, `apply`, `status` и
`rollback`. Apply отказывается переписывать неуправляемые файлы,
проверяет свободное место, записывает `/etc/fstab` атомарно и
применяет slice-лимиты к уже активной `user-0.slice`. Rollback удаляет
только точно помеченные файлы/строку fstab и восстанавливает
исходное `vm.swappiness`.

Приёмка memory guard:

1. Swapfile активен и указан в `/etc/fstab` ровно один раз.
2. `systemctl show user-0.slice` возвращает указанные лимиты.
3. `x-ui`, `nginx`, `ffknd-portal`, `trusttunnel`, `ttx-bridge` активны.
4. Listener-карта и public portal health не изменились.
5. Официальный TrustTunnel Client делает HTTPS-запрос через
   `tt.ffknd.ru:8443` и SOCKS-слушатель, не меняя маршруты хоста.

**Результат 2026-08-21: принято.** Swap активен, все четыре
slice-лимита совпадают с этой спецификацией, все пять сервисов
active. Официальный TrustTunnel Client 1.0.49 получил HTTPS через
`tt.ffknd.ru:8443` с выходом `95.182.85.8`; VLESS Reality smoke на `443`,
portal health, `ttx doctor` и server-side SOCKS e2e также прошли.

## 15. Журнал изменений

- **2026-08-21, REPO 0.2.2** — после host-wide OOM добавлен
  memory guard: 1 GiB swapfile, низкая swappiness и cgroup-лимиты
  только для `user-0.slice`. VPN-сервисы в `system.slice`, порты и
  конфиги TrustTunnel/Xray/Nginx не меняются.

- **2026-08-20, REPO 0.2.1 / portal 0.1.1** — исправлен рендер QR-кодов:
  общий CSS для SVG-иконок добавлял QR-матрице скруглённую обводку 1.8 px,
  из-за чего модули сливались и код не сканировался. В `.qr-output svg`
  добавлен изолированный reset `stroke: none; stroke-width: 0`; регрессионный
  тест фиксирует порядок каскада и обязательный reset. CSS и главный JS имеют
  query cache-buster версии portal, поэтому исправление не требует ручной
  очистки часового браузерного кеша.

- **2026-08-20, REPO 0.2.0** — production cutover принят: Nginx стал
  единственным владельцем `443/tcp`, портал доступен на `https://ffknd.ru`,
  Xray Reality сохранён за SNI-passthrough на loopback:10443, TrustTunnel
  продолжает работать на 8443/tcp+udp. Подтверждены trusted TLS, redirect www,
  точная listener-карта, restart persistence, реальный VLESS Reality e2e и
  целостность rollback backup. Удалён устаревший live `ExecStartPre`,
  вызывавший гонку с запуском x-ui.

- **2026-08-20, REPO 0.2.0-rc.1** — спецификация синхронизирована с
  фактической production-топологией: Nginx SNI router на `443/tcp`, portal
  через loopback 9443→8080, Xray Reality backend на 10443 и независимый
  TrustTunnel на 8443. Добавлены API-helper с online backup/rollback,
  one-shot cutover driver, настоящий Reality client smoke, REPO version и
  handoff. Статус RC сохраняется до §14.3.

- **2026-08-20** — в репозиторий добавлен ffknd portal на stdlib Python и
  SQLite: status checks, локальные web tools/AES-GCM notes/QR, RSS, короткие
  ссылки и технический справочник. Добавлены systemd/Compose-интеграция без
  отдельного публичного порта, `[reverse_proxy]` на loopback:8080, проверка
  разделения browser HTTP и VPN CONNECT на `443`, unit/security-тесты и
  сценарии отката. Эта запись описывает готовность к развёртыванию, а не факт
  установки на production-сервер.

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
  `setup_wizard` TrustTunnel (см. §12). Также создан приватный репозиторий
  `avarabey/CascadeVPN` на GitHub и запушена вся история.
- **2026-08-13** — репозиторий разложен по каталогам (`bridge/`, `install/`,
  `templates/`, `systemd/`, `tests/`, `compose/`, `deploy/`) в соответствии
  со структурой, уже подразумевавшейся в `install/ttx-install.sh` и README;
  добавлены отсутствовавшие файлы: `bridge/bridge.example.json`,
  `templates/compat.json`, `templates/vpn.base.toml.example`, все юниты
  `systemd/`, `compose/.env.example`, `compose/bridge.docker.json`,
  `compose/vpn.base.toml.example`, `.gitignore`; добавлен
  `deploy/deploy.sh` для удалённого развёртывания по SSH; создан этот файл.
