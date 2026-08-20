# Nginx SNI-router для ffknd.ru и Xray Reality

Эти файлы описывают фактическую bare-metal схему, где Nginx единолично
слушает публичный `443/tcp` и читает только SNI из TLS ClientHello:

```text
Internet :443/tcp
        |
        v
Nginx stream + ssl_preread
        |-- SNI ffknd.ru/www.ffknd.ru -> 127.0.0.1:9443 (Nginx HTTP + TLS)
        |                              `--> 127.0.0.1:8080 (portal HTTP)
        `-- любой другой/пустой SNI -> 127.0.0.1:10443 (Xray Reality)
```

На stream-уровне TLS не завершается и `proxy_protocol` не используется.
Поэтому Xray получает исходный ClientHello без добавленных байтов. Важно:
SNI (`serverName`) Reality-клиентов **не должен быть `ffknd.ru` или
`www.ffknd.ru`**, иначе точные правила намеренно отправят их в веб-ветку.
Текущий Reality SNI `cloud.ru` попадает в `default` и остаётся в VPN-ветке.

## Файлы и зависимости

- `modules-enabled/70-ffknd-stream.conf` добавляет отдельный `stream`-контекст
  через штатный Ubuntu include `/etc/nginx/modules-enabled/*.conf`;
- `stream-conf.d/ffknd-router.conf` содержит только SNI-маршрутизацию;
- `sites-available/ffknd.ru-http.conf` обслуживает ACME HTTP-01 на `80` и
  перенаправляет остальные запросы на HTTPS;
- `sites-available/ffknd.ru-portal.conf` завершает TLS только на
  `127.0.0.1:9443` и проксирует портал на `127.0.0.1:8080`.

Для Ubuntu установите Nginx и динамический stream-модуль:

```bash
sudo apt-get update
sudo apt-get install nginx libnginx-mod-stream certbot
test -e /etc/nginx/modules-enabled/50-mod-stream.conf
sudo nginx -V 2>&1 | grep -- --with-stream_ssl_preread_module
```

Если в существующем `nginx.conf` уже есть `stream {}`, не включайте
`70-ffknd-stream.conf`: добавьте в существующий контекст только
`include /etc/nginx/stream-conf.d/*.conf;`. Два верхнеуровневых stream-блока
Nginx не принимает.

## Подготовка сертификата без занятия 443

Сначала можно включить только HTTP-конфиг. Это не конфликтует с текущим
владельцем `443`:

```bash
sudo install -d -m 0755 /var/www/letsencrypt
sudo install -m 0644 nginx/sites-available/ffknd.ru-http.conf \
  /etc/nginx/sites-available/ffknd.ru-http.conf
sudo ln -sfn /etc/nginx/sites-available/ffknd.ru-http.conf \
  /etc/nginx/sites-enabled/ffknd.ru-http.conf
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/letsencrypt --cert-name ffknd.ru \
  -d ffknd.ru -d www.ffknd.ru
```

DNS `A` обоих имён должен вести на сервер, а `80/tcp` — быть доступен снаружи.
Сейчас у домена нет `AAAA`, поэтому шаблоны намеренно слушают только IPv4.
Добавляйте `listen [::]...` лишь одновременно с рабочим IPv6, firewall и
`AAAA`, иначе часть клиентов и ACME будет ходить на недоступный адрес.

## Переключение 443

Переключение неизбежно затрагивает действующие соединения, поэтому сначала
сохраните конфигурацию 3x-ui/Xray и держите открытой вторую SSH-сессию.

1. Убедитесь, что portal отвечает на `127.0.0.1:8080` и в его окружении стоят
   `PORTAL_PUBLIC_URL=https://ffknd.ru` и `PORTAL_COOKIE_SECURE=true`.
   `www.ffknd.ru` используется только как канонический `308` redirect.
2. В 3x-ui перенесите Reality inbound с публичного `443` на
   `127.0.0.1:10443`. Проверьте локальный listener до запуска Nginx.
3. Установите оставшиеся файлы и выполните только проверку синтаксиса:

   ```bash
   sudo install -d -m 0755 /etc/nginx/stream-conf.d
   sudo install -m 0644 nginx/modules-enabled/70-ffknd-stream.conf \
     /etc/nginx/modules-enabled/70-ffknd-stream.conf
   sudo install -m 0644 nginx/stream-conf.d/ffknd-router.conf \
     /etc/nginx/stream-conf.d/ffknd-router.conf
   sudo install -m 0644 nginx/sites-available/ffknd.ru-portal.conf \
     /etc/nginx/sites-available/ffknd.ru-portal.conf
   sudo ln -sfn /etc/nginx/sites-available/ffknd.ru-portal.conf \
     /etc/nginx/sites-enabled/ffknd.ru-portal.conf
   sudo nginx -t
   ```

4. После успешного `nginx -t` сделайте `systemctl reload nginx`, не
   `restart`: graceful reload сохраняет уже установленные длинные VPN-сессии.
   Для новых соединений `proxy_timeout 7d` и TCP keepalive действуют с обеих
   сторон stream-прокси.

Проверьте владельцев портов и обе ветки:

```bash
sudo ss -ltnp | grep -E ':(80|443|8080|9443|10443)\b'
curl --fail --resolve ffknd.ru:9443:127.0.0.1 \
  https://ffknd.ru:9443/api/health
curl --fail https://ffknd.ru/api/health
openssl s_client -connect ffknd.ru:443 -servername ffknd.ru \
  -verify_hostname ffknd.ru </dev/null
```

Ожидается: Nginx слушает публичные `80/tcp` и `443/tcp`, Nginx HTTP — только
`127.0.0.1:9443`, portal — только `127.0.0.1:8080`, Xray — только
`127.0.0.1:10443`. Проверка веб-ветки не доказывает работу Reality: после
переключения запустите root-only smoke с реальным существующим клиентом:

```bash
sudo env REALITY_SMOKE_INBOUND_TAG=in-443-tcp-2 \
  ./tests/reality-e2e-smoke.sh
```

Скрипт только читает `/usr/local/x-ui/bin/config.json`, выбирает включённого
клиента указанного VLESS Reality inbound и создаёт приватный временный Xray
client config. Отдельный Xray слушает случайный порт только на `127.0.0.1`, а
`curl` идёт через него к Cloudflare trace. Тело ответа, Xray/curl logs, UUID,
Reality keys и short ID не печатаются. Временный каталог имеет режим `0700`,
файлы — `0600`, core dump отключён; trap останавливает Xray и удаляет файлы при
обычном завершении и сигналах. Если public key отсутствует, helper выводит его
из private key внутри Python: вызов `xray x25519 -i` не используется, чтобы
private key не появился в argv. При нескольких Reality inbound переменная с
tag обязательна; при одном её можно опустить.

Успешный тест подтверждает TCP-цепочку `SOCKS -> VLESS Reality -> public :443
-> Nginx default passthrough -> 127.0.0.1:10443 -> HTTPS`. Он намеренно падает,
если первый Reality `serverName` совпадает с `ffknd.ru`/portal route. Это не
проверка UDP и не проверка TrustTunnel. После smoke всё равно проверьте обычный
пользовательский клиент. Не публикуйте `8080`, `9443` или `10443` в
firewall/Docker.

Для продления сертификата используйте тот же webroot и проверяйте таймер:

```bash
sudo certbot renew --dry-run
systemctl list-timers certbot.timer
```

После продления сертификата Nginx должен получить graceful reload. У пакета
Certbot/Nginx это обычно делает deploy hook; явно настроенный безопасный hook:

```text
deploy_hook = nginx -t && systemctl reload nginx
```

## Откат

Если проблема только в портале, Reality-ветку можно оставить через Nginx и
исправить `9443/8080` отдельно. Для полного отката production-хоста сначала
остановите Nginx, затем верните Xray Reality на исходный публичный `443` через
root-only rollback state; обратный порядок приведёт к конфликту bind:

```bash
sudo systemctl stop nginx
sudo python3 /root/ffknd-cutover-tools/xui_portal_cutover.py rollback
sudo ss -ltnp | grep ':443\b'
sudo systemctl is-active x-ui trusttunnel ttx-bridge
```

Остановка Nginx обрывает текущие stream-соединения, поэтому полный rollback
планируйте как короткое окно недоступности. Не запускайте Nginx с production
stream-конфигурацией, пока Xray снова владеет публичным `443`.
