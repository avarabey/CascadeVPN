# CascadeVPN / ffknd portal

Текущая версия REPO: [0.2.1](REPO_VERSION.md). Полная нормативная
конфигурация: [SPEC.md](SPEC.md). Точка безопасного продолжения эксплуатации:
[HANDOFF.md](HANDOFF.md).

Production `ffknd.ru` использует Nginx `ssl_preread` на `443/tcp`: SNI
`ffknd.ru`/`www.ffknd.ru` идёт в нейтральный портал, любой другой SNI —
byte-for-byte в Xray VLESS Reality. TrustTunnel работает независимо на
`8443/tcp+udp` и передаёт трафик в routing 3x-ui через SOCKS ingress.

**Ни один файл исходных репозиториев не изменяется.** Проект живёт в отдельных
каталогах (`/opt/ttx`, `/etc/ttx`) и взаимодействует с upstream только через их
публичные контракты.

---

## 1. Как это работает в production

```
Internet :443/tcp ──► Nginx stream ssl_preread
                       ├── ffknd.ru / www.ffknd.ru
                       │      └──► 127.0.0.1:9443 TLS
                       │                └──► portal 127.0.0.1:8080
                       └── default ──► Xray Reality 127.0.0.1:10443

Internet :8443/tcp+udp ──► TrustTunnel ──► Xray SOCKS 127.0.0.1:10800
```

Nginx не завершает TLS на Reality-ветке и не использует PROXY protocol.
Reality SNI — только `cloud.ru*`; домены портала для VPN-профиля запрещены.
Reference/Compose-схема с TrustTunnel как владельцем `443/tcp+udp` сохранена
для новых окружений, но generic installer нельзя запускать на текущем live-хосте.

## 2. Что добавляет этот проект

| Компонент | Назначение |
|---|---|
| `bridge/ttx_bridge.py` | Демон согласования: держит inbound в панели и секцию `forward_protocol` в `vpn.toml` в согласованном состоянии |
| `templates/vpn.base.toml.example` | Базовый конфиг оператора — источник правды, мост его не трогает |
| `templates/compat.json` | Матрица совместимости версий upstream + контракт «какие таблицы конфига мы считаем своими» |
| `systemd/trusttunnel.service.d/10-ttx-overlay.conf` | Drop-in к юниту TrustTunnel: только порядок запуска, без изменения конфига в `ExecStartPre`. Оригинальный юнит остаётся нетронутым |
| `install/ttx-install.sh` | Ставит оба проекта их же штатными установщиками и разворачивает обвязку поверх |
| `compose/` | Docker-вариант: образы берутся из upstream по тегу, без вендоринга кода |
| `tests/e2e-smoke.sh` | Сквозная проверка цепочки |
| `portal/` | Личный портал на стандартной библиотеке Python 3.11+ и SQLite, без внешних Python-зависимостей |
| `systemd/ffknd-portal.service` | Изолированный bare-metal сервис портала на loopback:8080 |
| `tests/port443-smoke.sh` | Внешняя проверка обычного HTTPS и VPN CONNECT через один `443` |

### Что есть в ffknd portal

- статус HTTP(S)-сервисов с фоновыми проверками времени ответа;
- локальные веб-инструменты: JSON, Base64, UUID, timestamp, URL encode/decode,
  генератор паролей и QR-кодов;
- блокнот, который шифруется в браузере AES-GCM и хранится только в
  `localStorage`; открытый текст и мастер-пароль серверу не передаются;
- RSS/Atom-лента с фоновым обновлением;
- короткие ссылки `/s/<code>` со счётчиком переходов и локальный QR-генератор;
- встроенные памятки по Git, Linux, Docker, HTTP и регулярным выражениям.

SQLite хранит серверные сессии, настройки проверок, RSS-источники и короткие
ссылки. Заметки в эту базу не записываются. Управление состоянием, RSS и
короткими ссылками требует входа; публичная страница остаётся нейтральной.

### Принцип «расширение, а не модификация»

1. **3x-ui** — только REST API (`/login`, `/panel/api/inbounds/*`). Ни база
   `x-ui.db`, ни Go-код, ни шаблоны панели не трогаются. Всё, что делает мост,
   оператор может сделать руками в веб-интерфейсе.
2. **TrustTunnel** — генерируется только `vpn.toml`, и только одна таблица в нём.
   Всё остальное копируется из вашего базового файла байт в байт; управляемый
   блок отделён маркером. Бинарники и `scripts/` берутся из релизов как есть.
3. **systemd** — drop-in-файлы, а не правка `trusttunnel.service.template`.
4. **Docker** — `build.context` указывает на upstream-репозиторий по git-тегу,
   поэтому форк не нужен.

Совместимость отслеживается через `compat.json`, функциональные проверки API и
`ttx doctor`. Обновления upstream всё равно сначала проверяйте на стенде.

## 3. Установка (bare-metal)

### Быстрый старт (Ubuntu)

Один скрипт делает всё, кроме мастера TrustTunnel (см. ниже, шаг 1) —
подробности и флаги в [SPEC.md §12](SPEC.md#12-развёртывание):

```bash
git clone https://github.com/avarabey/CascadeVPN.git ttx
cd ttx
sudo ./deploy/bootstrap-ubuntu.sh
```

Если `/opt/trusttunnel/vpn.toml` ещё нет (мастер не запускали), скрипт
остановится и попросит прогнать `setup_wizard` один раз — дальше можно
перезапустить его же командой. При новой установке 3x-ui bootstrap берёт
сгенерированный API token из `/etc/x-ui/install-result.env`; для уже
существующей панели передайте `--panel-api-token` или логин с паролем.
Если `/etc/ttx/portal.env` ещё не содержит хеш пароля, bootstrap установит
портал, но оставит его выключенным; настройте хеш по шагу 5 ниже и включите
`ffknd-portal` отдельно.

### Вручную по шагам

```bash
git clone https://github.com/avarabey/CascadeVPN.git ttx && cd ttx
sudo ./install/ttx-install.sh          # ставит 3x-ui + TrustTunnel + обвязку

# 1) мастер TrustTunnel: сертификаты, пользователи, listen_address
cd /opt/trusttunnel && sudo ./setup_wizard
sudo cp trusttunnel.service.template /etc/systemd/system/trusttunnel.service

# 2) забрать созданный мастером конфиг как базовый и убрать оригинал из-под ног
sudo cp /opt/trusttunnel/vpn.toml /etc/ttx/vpn.base.toml

# 3) вписать доступы к панели
sudo nano /etc/ttx/bridge.json         # api_token (рекомендуется) либо username/password

# 4) убедиться, что в /etc/ttx/vpn.base.toml есть origin портала
sudo nano /etc/ttx/vpn.base.toml
```

В редакторе добавьте таблицу, если её ещё нет:

```toml
[reverse_proxy]
server_address = "127.0.0.1:8080"
path_mask = "/"
h3_backward_compatibility = false
```

После сохранения продолжите:

```bash
# 5) создать отдельный пароль входа в портал
cd /opt/ttx/portal
python3 -m app hash-password            # Python 3.11+; скопируйте выведенный хеш
sudo cp -n /etc/ttx/portal.env.example /etc/ttx/portal.env
sudo chmod 0600 /etc/ttx/portal.env
sudo nano /etc/ttx/portal.env           # PORTAL_PASSWORD_HASH и PORTAL_PUBLIC_URL

# 6) проверить и согласовать
sudo ttx doctor
sudo ttx reconcile --dry-run           # показать, что получится
sudo ttx reconcile

# 7) запустить
sudo systemctl daemon-reload
sudo systemctl enable --now x-ui ffknd-portal trusttunnel ttx-bridge
sudo /opt/ttx/tests/e2e-smoke.sh
```

`install/ttx-install.sh` сам добавляет `[reverse_proxy]`, если
`/etc/ttx/vpn.base.toml` уже существует. Он обрамляет свой блок маркерами и
дополнительно сохраняет точечный snapshot `vpn.base.toml.portal-bak`.
Штатный откат удаляет только помеченный блок командой
`ffknd-portal-config remove /etc/ttx/vpn.base.toml`, не заменяя файл
старым snapshot. При последовательности выше базовый файл появляется
после установщика, поэтому блок показан явно.

Пустой или неверного формата `PORTAL_PASSWORD_HASH` считается ошибкой:
bootstrap не включает и, если нужно, останавливает `ffknd-portal`.
TrustTunnel не имеет зависимостей `Wants=` или `Requires=` на портал и поэтому
запускается независимо.
Если bootstrap установил отдельный Python 3.11, используйте для
`hash-password` путь к интерпретатору, который он напечатал и записал в
`ffknd-portal.service`.

Выдача клиента — штатным способом TrustTunnel:

```bash
cd /opt/trusttunnel
./trusttunnel_endpoint vpn.toml hosts.toml -c alice -a vpn.example.com \
    --generate-client-random-prefix
```

### Docker

```bash
cd compose
cp .env.example .env
cp bridge.docker.json bridge.json          # впишите API token либо username/password панели
cp vpn.base.toml.example vpn.base.toml
cp credentials.toml.example credentials.toml
cp hosts.toml.example hosts.toml
cp rules.toml.example rules.toml
mkdir -p certs                              # положите сюда fullchain.pem/privkey.pem
# замените CHANGE_ME и vpn.example.com во вновь созданных файлах

# сгенерируйте хеш и вставьте его в compose/.env как PORTAL_PASSWORD_HASH=...
cd ../portal && python3 -m app hash-password && cd ../compose
nano .env

# сначала поднимите только панель, войдите через SSH-туннель на 127.0.0.1:2053,
# смените начальные реквизиты и создайте API token в Settings → Security
docker compose up -d x-ui
nano bridge.json                           # вставьте API token

docker compose up -d --build
```

В Compose контейнер `portal` работает от пользователя `portal`, использует
`network_mode: "service:trusttunnel"` и слушает общий с TrustTunnel loopback на
`127.0.0.1:8080`. У него нет `ports`: наружу публикуется только `443/tcp+udp`
контейнера TrustTunnel. Обычный `docker compose restart trusttunnel`
сохраняет namespace. Если же контейнер TrustTunnel пересоздаётся, пересоздайте
оба контейнера за один запуск:
`docker compose up -d --force-recreate trusttunnel portal`.

После развёртывания проверьте обе ветки `443` с внешней машины:

```bash
export PORTAL_PUBLIC_URL=https://ffknd.ru
export TT_CLIENT_USERNAME=alice
read -r -s -p 'TrustTunnel password: ' TT_CLIENT_PASSWORD; echo
export TT_CLIENT_PASSWORD
./tests/port443-smoke.sh
unset TT_CLIENT_PASSWORD
```

Без клиентских реквизитов доступен только неполный web-тест:
`PORT443_WEB_ONLY=1 ./tests/port443-smoke.sh`. Детали и откат — в
[RUNBOOK.md](RUNBOOK.md#проверка-общего-публичного-443).

## 4. Эксплуатация

```bash
ttx status              # состояние inbound и указателя в vpn.toml
ttx reconcile           # разовая синхронизация
ttx doctor              # проверки перед запуском
journalctl -u ttx-bridge -f
journalctl -u ffknd-portal -f
curl --fail http://127.0.0.1:8080/api/health
```

Мост отслеживает дрейф: если в панели кто-то поменял порт ingress'а или
выключил inbound, при следующем цикле состояние восстановится, `vpn.toml`
перегенерируется и TrustTunnel перезапустится. Перед записью итог проходит
TOML-валидацию; после restart мост проверяет `systemctl is-active`. Если restart
или health check не удался, файл атомарно возвращается в состояние перед
apply и прежняя конфигурация запускается повторно. Поставьте `ingress.manage: false`,
если хотите, чтобы панель считалась источником правды, а мост только подстраивал
`vpn.toml`.

### Карта портов

| Порт | Кто слушает | Доступ |
|---|---|---|
| 443/tcp + 443/udp | TrustTunnel endpoint | публичный |
| 8080/tcp | ffknd portal | **только loopback / общий namespace Compose** |
| 10800/tcp | Xray inbound TTX-Ingress | **только loopback** |
| 2053 (или сгенерированный) | панель 3x-ui | рекомендуется loopback + SSH-туннель |
| 1987 | метрики TrustTunnel | loopback |

Прочие inbound'ы 3x-ui (VLESS/Reality и т. п.) продолжают работать на своих
портах параллельно — связка их не отменяет.

## 5. Ограничения, которые нужно знать заранее

- **ICMP.** SOCKS5 не переносит ICMP. Если в `vpn.base.toml` включена секция
  `[icmp]`, пинги пойдут напрямую с сервера, минуя routing 3x-ui. Отключите
  `[icmp]`, если нужна строгая политика «весь трафик через панель».
- **UDP.** Требует, чтобы у inbound было `udp: true` (мост ставит это сам) и
  чтобы SOCKS5-клиент endpoint'а использовал UDP ASSOCIATE. Проверьте на своей
  версии: `e2e-smoke.sh` + клиентский тест QUIC. Если UDP не проходит, DNS и
  HTTP/3 у клиентов деградируют на TCP.
- **Учёт трафика.** Через один ingress все пользователи TrustTunnel выглядят для
  панели как один клиент: статистика будет агрегированной. Пофамильный учёт
  ведётся на стороне TrustTunnel (`credentials.toml` + метрики Prometheus на
  :1987). Разделение по политикам — см. раздел 6.
- **Перезапуск Xray.** Любое изменение в панели перезапускает xray и на секунду-две
  рвёт активные соединения через ingress. Само TLS-соединение клиента с
  endpoint'ом при этом сохраняется.
- **Петли.** `allow_private_network_connections` обязан быть `false`, иначе
  клиент через туннель дотянется до панели на loopback. `ttx doctor` это
  проверяет, а `guard_loop` отказывается писать конфиг, если порт ingress'а
  совпал с `listen_address`.
- **Исходящие URL портала.** RSS и мониторинг по умолчанию разрешают только
  публичные адреса на портах 80/443. Не включайте `PORTAL_ALLOW_PRIVATE_URLS`
  без необходимости: это расширяет доступ портала к внутренней сети.
- **Локальный блокнот.** Мастер-пароль восстановить нельзя, а данные привязаны
  к хранилищу конкретного браузера. Пароль входа в портал его не заменяет.

## 6. Дальнейшее развитие (не входит в текущую поставку)

- **Политики по группам.** Несколько ingress-inbound'ов (по одному на тариф) +
  несколько экземпляров endpoint'а через systemd-шаблон `trusttunnel@.service`,
  каждый со своим SNI-хостом и своим upstream'ом. Даёт разные routing-правила и
  раздельную статистику для разных групп пользователей.
- **Синхронизация пользователей.** Двусторонняя связка `credentials.toml` ↔
  клиенты панели с проверкой квот и сроков (отключение пользователя в панели
  удаляет его из credentials и перечитывает конфиг).
- **Резервное копирование портала.** Автоматический экспорт SQLite и
  зашифрованного локального блокнота пока не настроен.

## 7. Лицензии

TrustTunnel — Apache-2.0, 3x-ui — GPL-3.0. Проект их не включает и не
модифицирует, а вызывает как внешние компоненты, поэтому копилефт 3x-ui на код
обвязки не распространяется. При распространении собранного дистрибутива
приложите оригинальные лицензии обоих проектов.
