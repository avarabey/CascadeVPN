# ttx — обвязка TrustTunnel → 3x-ui

Трафик приходит на сервер по протоколу TrustTunnel (маскируется под обычный HTTPS),
терминируется endpoint'ом и целиком передаётся в inbound Xray, которым управляет
3x-ui. Дальше работают штатные routing-правила и outbound'ы панели.

**Ни один файл исходных репозиториев не изменяется.** Проект живёт в отдельных
каталогах (`/opt/ttx`, `/etc/ttx`) и взаимодействует с upstream только через их
публичные контракты.

---

## 1. Как это работает

```
клиент TrustTunnel
      │  TLS / HTTP2 / QUIC :443     (неотличимо от обычного HTTPS)
      ▼
┌─────────────────────────┐
│ trusttunnel_endpoint    │  upstream-бинарь, конфиг vpn.toml
│ [forward_protocol.socks5]  ◄── эту секцию генерирует ttx-bridge
└──────────┬──────────────┘
           │  SOCKS5 (TCP + UDP ASSOCIATE) на 127.0.0.1:10800
           ▼
┌─────────────────────────┐
│ Xray inbound "TTX-Ingress"   протокол socks, listen 127.0.0.1
│ (создан и поддерживается через REST API 3x-ui)
└──────────┬──────────────┘
           │
           ▼   routing 3x-ui: правила, geoip/geosite, балансировщики
   outbound: direct / WARP / VLESS-цепочка / blackhole
```

Точка стыковки — документированный параметр TrustTunnel
`[forward_protocol.socks5]`. Endpoint по нему отдаёт **весь** туннелированный
TCP и UDP наверх, в SOCKS5-прокси. Роль этого прокси и играет inbound Xray.
Никакого перехвата трафика, iptables-магии или патчей не требуется.

## 2. Что добавляет этот проект

| Компонент | Назначение |
|---|---|
| `bridge/ttx_bridge.py` | Демон согласования: держит inbound в панели и секцию `forward_protocol` в `vpn.toml` в согласованном состоянии |
| `templates/vpn.base.toml.example` | Базовый конфиг оператора — источник правды, мост его не трогает |
| `templates/compat.json` | Матрица совместимости версий upstream + контракт «какие таблицы конфига мы считаем своими» |
| `systemd/trusttunnel.service.d/10-ttx-overlay.conf` | Drop-in к юниту TrustTunnel: порядок запуска и `ExecStartPre` с синхронизацией. Оригинальный юнит остаётся нетронутым |
| `install/ttx-install.sh` | Ставит оба проекта их же штатными установщиками и разворачивает обвязку поверх |
| `compose/` | Docker-вариант: образы берутся из upstream по тегу, без вендоринга кода |
| `tests/e2e-smoke.sh` | Сквозная проверка цепочки |

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
подробности и флаги в [SPEC.md §11](SPEC.md#11-развёртывание):

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

# 4) проверить и согласовать
sudo ttx doctor
sudo ttx reconcile --dry-run           # показать, что получится
sudo ttx reconcile

# 5) запустить
sudo systemctl daemon-reload
sudo systemctl enable --now x-ui trusttunnel ttx-bridge
sudo /opt/ttx/tests/e2e-smoke.sh
```

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

# сначала поднимите только панель, войдите через SSH-туннель на 127.0.0.1:2053,
# смените начальные реквизиты и создайте API token в Settings → Security
docker compose up -d x-ui
nano bridge.json                           # вставьте API token

docker compose up -d --build
```

## 4. Эксплуатация

```bash
ttx status              # состояние inbound и указателя в vpn.toml
ttx reconcile           # разовая синхронизация
ttx doctor              # проверки перед запуском
journalctl -u ttx-bridge -f
```

Мост отслеживает дрейф: если в панели кто-то поменял порт ingress'а или
выключил inbound, при следующем цикле состояние восстановится, `vpn.toml`
перегенерируется и TrustTunnel перезапустится. Поставьте `ingress.manage: false`,
если хотите, чтобы панель считалась источником правды, а мост только подстраивал
`vpn.toml`.

### Карта портов

| Порт | Кто слушает | Доступ |
|---|---|---|
| 443/tcp + 443/udp | TrustTunnel endpoint | публичный |
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

## 6. Дальнейшее развитие (не входит в текущую поставку)

- **Политики по группам.** Несколько ingress-inbound'ов (по одному на тариф) +
  несколько экземпляров endpoint'а через systemd-шаблон `trusttunnel@.service`,
  каждый со своим SNI-хостом и своим upstream'ом. Даёт разные routing-правила и
  раздельную статистику для разных групп пользователей.
- **Синхронизация пользователей.** Двусторонняя связка `credentials.toml` ↔
  клиенты панели с проверкой квот и сроков (отключение пользователя в панели
  удаляет его из credentials и перечитывает конфиг).
- **Общий порт 443.** Xray-inbound с fallback по SNI на 443, отдающий сырой TCP
  на TrustTunnel для «своего» имени хоста — позволит держать на 443 и туннель, и
  обычный сайт.

## 7. Лицензии

TrustTunnel — Apache-2.0, 3x-ui — GPL-3.0. Проект их не включает и не
модифицирует, а вызывает как внешние компоненты, поэтому копилефт 3x-ui на код
обвязки не распространяется. При распространении собранного дистрибутива
приложите оригинальные лицензии обоих проектов.
