# Runbook

Текущая production-конфигурация определяется [SPEC.md](SPEC.md), версия —
[REPO_VERSION.md](REPO_VERSION.md). Перед изменением production-топологии
сначала откройте [HANDOFF.md](HANDOFF.md); generic installer не запускать.

Команды ниже относятся к установленному серверу. Для Docker выполняйте их из
`compose/`; для проверки публичного `443` используйте отдельную машину вне
сервера.

## Быстрая проверка состояния

Bare-metal:

```bash
systemctl status nginx trusttunnel ffknd-portal ttx-bridge x-ui
ss -ltnup | grep -E ':(443|8443|8080|9443|10443|10800)\b'
curl --fail http://127.0.0.1:8080/api/health
ttx status
```

Production: `443/tcp` принадлежит Nginx, `8443/tcp+udp` —
TrustTunnel, portal — `127.0.0.1:8080`, Nginx TLS — `127.0.0.1:9443`, Xray
Reality — `127.0.0.1:10443`, SOCKS ingress — `127.0.0.1:10800`.

## Memory guard на production VM

На VM с 1 GiB RAM активен REPO 0.2.2 memory guard. Он не меняет
VPN-порты и не перезапускает сервисы: 1 GiB swap даёт ядру
аварийный запас, а `user-0.slice` не даёт отсоединённым
root/SSH-процессам вытеснить `system.slice`.

Проверка:

```bash
sudo /root/ffknd-memory-hardening/deploy/harden-memory.sh status
free -h
systemctl show user-0.slice \
  -p MemoryHigh -p MemoryMax -p MemorySwapMax -p TasksMax
```

Перед повторным apply сначала выполняйте `dry-run`. Откат
удаляет только managed-файлы/строку fstab и восстанавливает
прежнюю swappiness:

```bash
sudo /root/ffknd-memory-hardening/deploy/harden-memory.sh dry-run
sudo /root/ffknd-memory-hardening/deploy/harden-memory.sh rollback
```

После rollback сразу повторите быструю проверку сервисов. Не
удаляйте `/var/lib/ffknd-memory` и не редактируйте fstab вручную:
скрипт проверяет ownership и managed-маркеры.

Docker:

```bash
docker compose ps
docker compose logs --tail=100 portal trusttunnel bridge
docker compose exec portal python3 -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/api/health').read().decode())"
```

`docker compose restart trusttunnel` не меняет сетевой namespace. При
принудительном пересоздании TrustTunnel пересоздайте и `portal`, чтобы он
присоединился к новому namespace:

```bash
docker compose up -d --force-recreate trusttunnel portal
```

## VPN работает, но портал не открывается

1. Проверьте origin напрямую:

   ```bash
   systemctl status ffknd-portal
   journalctl -u ffknd-portal -n 100 --no-pager
   curl --fail http://127.0.0.1:8080/api/health
   ```

2. Если сервис не стартует с сообщением о `PORTAL_PASSWORD_HASH`, создайте хеш
   и сохраните его в root-only env-файле:

   ```bash
   cd /opt/ttx/portal
   python3 -m app hash-password
   sudo cp -n /etc/ttx/portal.env.example /etc/ttx/portal.env
   sudo chmod 0600 /etc/ttx/portal.env
   sudoedit /etc/ttx/portal.env
   sudo systemctl enable --now ffknd-portal
   ```

   Впишите полный результат как `PORTAL_PASSWORD_HASH=...`; также проверьте
   `PORTAL_PUBLIC_URL=https://ffknd.ru`. Порталу нужен Python 3.11+.

3. Проверьте production TLS-terminator и Nginx config:

   ```bash
   nginx -t
   curl --fail --resolve ffknd.ru:9443:127.0.0.1 \
     https://ffknd.ru:9443/api/health
   journalctl -u nginx -n 100 --no-pager
   ```

4. Если loopback-origin и `9443` отвечают, но внешний HTTPS нет,
   сверьте `nginx/stream-conf.d/ffknd-router.conf`, DNS A-record и listener
   `0.0.0.0:443`. Production-портал не идёт через TrustTunnel;
   `[reverse_proxy]` нужен только reference/Compose-схеме.

## Портал открывается, но вход не работает

- `PORTAL_PASSWORD_HASH` — это результат `python3 -m app hash-password`, а не
  пароль в открытом виде. После его замены перезапустите сервис. Уже выданные
  сессии это автоматически не отзывает; по умолчанию они истекают через 12
  часов, а для немедленного отзыва нужно удалить строки таблицы `sessions` из
  SQLite при остановленном portal.
- `PORTAL_PUBLIC_URL` должен точно соответствовать внешней схеме и host,
  иначе проверка Origin отклонит изменяющие запросы.
- При HTTPS оставляйте `PORTAL_COOKIE_SECURE=true`. Для временной локальной
  диагностики по HTTP можно отключить флаг только в непубличном окружении.
- Ограничение входа — восемь неудачных попыток с одного адреса за пять минут.

## RSS или проверки доступности не обновляются

Смотрите `journalctl -u ffknd-portal -f` и проверьте DNS с сервера. По умолчанию
портал разрешает исходящие HTTP(S)-адреса только на портах 80/443 и блокирует
loopback, RFC1918, link-local и прочие непубличные диапазоны. Это защита от
SSRF, а не сбой сети.

Для осознанного мониторинга внутренних сервисов задайте в
`/etc/ttx/portal.env`:

```dotenv
PORTAL_ALLOW_PRIVATE_URLS=true
PORTAL_ALLOWED_OUTBOUND_PORTS=80,443,8080
```

Затем перезапустите `ffknd-portal`. Link-local metadata endpoints остаются
запрещены даже в этом режиме.

## Зашифрованный блокнот не открывается

Блокнот не хранится в SQLite и не зависит от пароля входа в портал. Его
шифротекст находится в `localStorage` текущего браузерного профиля; ключ
выводится из отдельного мастер-пароля и существует только в браузере.
Восстановить забытый мастер-пароль невозможно. Очистка данных сайта или кнопка
удаления блокнота необратимо удаляет локальную копию.

## Клиенты подключаются, но интернета нет

1. `ttx status` — есть ли ingress и указывает ли на него `vpn.toml`.
2. `ss -ltnp | grep -E ':10800\b'` — слушает ли Xray. Если нет, inbound выключен
   в панели или Xray не стартовал (журнал панели → Xray Status).
3. `curl --socks5-hostname 127.0.0.1:10800 https://ifconfig.me` — работает ли
   цепочка после моста. Если нет, проблема в routing/outbound 3x-ui.
4. Если п. 3 работает, смотрите `journalctl -u trusttunnel -f`.

Портал не находится в этом data path. Его остановка не должна исправлять или
ломать VPN CONNECT.

## Не работает QUIC / медленный DNS у клиентов

Проверьте `udp: true` у ingress-inbound и поддержку UDP ASSOCIATE в вашей
версии endpoint. Временный обход: включить у клиента DNS-upstream по DoT/DoH
через TCP.

## После обновления 3x-ui пропал inbound

Мост восстановит его в течение `interval_secs`. Ускорить: `ttx reconcile`.

## После обновления TrustTunnel сервис не стартует

```bash
ttx doctor                    # покажет несоответствие версии compat.json
systemctl status trusttunnel
```

Если сбой возник после `ttx reconcile`, мост уже проверил restart через
`systemctl is-active`: при ошибке он атомарно восстанавливает состояние `vpn.toml`
непосредственно до apply и повторно запускает прежнюю конфигурацию. Проверьте
результат в `journalctl -u ttx-bridge -n 100 --no-pager`; код 4 означает, что новый
конфиг не был принят.

`/opt/trusttunnel/vpn.toml.ttx-bak` — снимок для аварийного разбора, а не
основной механизм rollback. Если в журнале сказано, что не запустилась и
прежняя конфигурация, не перезаписывайте файл вслепую: сохраните текущий
экземпляр, сверьте его с `.ttx-bak` и `vpn.base.toml`, затем восстановите
проверенную копию. Если причина в обновлённом endpoint, установите предыдущую
версию через `install.sh -V <version>`.

## Проверка общего публичного 443

Запускайте `tests/port443-smoke.sh` с внешней машины после изменения
TrustTunnel, reverse proxy или портала. Скрипт проверяет главную страницу,
`/api/health` и HTTPS через VPN `CONNECT` на том же `443`:

```bash
export PORTAL_PUBLIC_URL=https://ffknd.ru
read -r -p 'TrustTunnel user: ' TT_CLIENT_USERNAME
read -r -s -p 'TrustTunnel password: ' TT_CLIENT_PASSWORD; echo
export TT_CLIENT_USERNAME TT_CLIENT_PASSWORD
./tests/port443-smoke.sh
unset TT_CLIENT_PASSWORD
```

Если клиентских реквизитов ещё нет, можно проверить только веб-ветку, но это
не является доказательством сохранности VPN:

```bash
PORTAL_PUBLIC_URL=https://ffknd.ru PORT443_WEB_ONLY=1 ./tests/port443-smoke.sh
```

### Реальный VLESS Reality smoke после SNI-cutover

На bare-metal схеме с Nginx `ssl_preread` одной проверки веб-ветки мало.
Скопируйте `tests/reality-e2e-smoke.sh` и `tests/reality_smoke_config.py` в
root-owned checkout на сервере и выполните:

```bash
sudo env REALITY_SMOKE_INBOUND_TAG=in-10443-tcp \
  ./tests/reality-e2e-smoke.sh
```

Smoke не меняет 3x-ui/Xray: он читает active config, поднимает отдельный
loopback SOCKS + Xray client и делает HTTPS-запрос через публичный Reality
endpoint. UUID и Reality-параметры существуют только в временном каталоге
`0700`, не передаются через argv и не выводятся; cleanup останавливает процесс
и удаляет файлы. Если Reality inbound в active config ровно один, переменную
`REALITY_SMOKE_INBOUND_TAG` можно не задавать. Подробная модель угроз и границы
проверки описаны в `nginx/README.md`.

## Откат только портала

Аварийный и наиболее безопасный шаг — остановить origin, не трогая
TrustTunnel и сгенерированный SOCKS5-upstream:

```bash
sudo systemctl disable --now ffknd-portal
```

Веб-ветка перестанет отвечать, но VPN `CONNECT` должен продолжить работу.
Проверьте его реальным клиентом. SQLite остаётся в
`/var/lib/ffknd-portal/portal.db` и может быть использована после повторного
включения сервиса.

Для полного отката bare-metal удалите только помеченный установщиком
блок `[reverse_proxy]`. Утилита откажется трогать чужую или
изменённую внутри маркеров секцию:

```bash
sudo ffknd-portal-config remove /etc/ttx/vpn.base.toml
sudo ttx doctor
sudo ttx reconcile --dry-run
sudo ttx reconcile
```

`vpn.base.toml.portal-bak` — точечный snapshot на момент установки,
а не штатный rollback: его полное восстановление затрёт все более
поздние правки оператора. Если блок был добавлен вручную и не имеет
маркеров ffknd portal, сначала сохраните текущий файл и удалите
вручную только таблицу `[reverse_proxy]` с её тремя ключами. Не удаляйте
управляемую секцию `[forward_protocol.socks5]` из итогового конфига.

В Docker экстренный откат — `docker compose stop portal`. Для полного отката
сохраните и отредактируйте `compose/vpn.base.toml`, перезапустите `bridge`,
убедитесь, что он обновил общий volume, и только затем перезапустите
`trusttunnel`. Публичный `443` никогда не передавайте другому контейнеру.

## Полное отключение обвязки

Этот сценарий отключает и портал, и интеграцию с 3x-ui; он не нужен для
обычного отката веб-части.

```bash
systemctl disable --now ffknd-portal ttx-bridge ttx-reconcile.timer
cp /opt/trusttunnel/vpn.toml /opt/trusttunnel/vpn.toml.before-ttx-disable
mv /etc/systemd/system/trusttunnel.service.d/10-ttx-overlay.conf \
   /etc/systemd/system/trusttunnel.service.d/10-ttx-overlay.conf.disabled
systemctl daemon-reload
# Уберите управляемую секцию после маркера ttx-bridge из vpn.toml вручную;
# резервная копия уже сохранена строкой выше.
systemctl restart trusttunnel
```

Оба upstream-проекта остаются доступными: обвязка не патчит их исходники.
