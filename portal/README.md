# ffknd portal

Небольшой персональный портал без внешних runtime-зависимостей: мониторинг
HTTP-сервисов, RSS/Atom, короткие ссылки, локальные веб-инструменты,
зашифрованный браузерный блокнот и технические шпаргалки.

Backend использует стандартную библиотеку Python и SQLite. Заметки шифруются
в браузере через PBKDF2 + AES-256-GCM; мастер-пароль и открытый текст на сервер
не отправляются.

## Локальный запуск

Нужен Python 3.11 или новее.

```bash
cd portal
python3 -m app hash-password

export PORTAL_PASSWORD_HASH='вставьте полученный хеш'
export PORTAL_COOKIE_SECURE=false
export PORTAL_PUBLIC_URL=http://127.0.0.1:8080
export PORTAL_DB=/tmp/ffknd-portal.db
python3 -m app serve
```

После запуска откройте `http://127.0.0.1:8080/`.

## Docker Compose

В составе CascadeVPN контейнер не публикует собственный порт. Он запускается
пользователем `portal`, разделяет network namespace с TrustTunnel и слушает
только общий `127.0.0.1:8080`. TrustTunnel остаётся единственным процессом на
публичном `443/tcp+udp` и передаёт порталу только обычные HTTP-запросы.

```bash
cd compose
cp .env.example .env
cd ../portal && python3 -m app hash-password
# сохраните хеш в compose/.env как PORTAL_PASSWORD_HASH
cd ../compose
docker compose up -d --build
```

Пустой `PORTAL_PASSWORD_HASH` приводит к безопасному отказу запуска.

## Основные переменные

- `PORTAL_PASSWORD_HASH` — обязательный PBKDF2-хеш администратора.
- `PORTAL_DB` — путь к SQLite; по умолчанию приложение использует `/data/portal.db`,
  bare-metal пример и systemd unit используют доступный на запись
  `/var/lib/ffknd-portal/portal.db`.
- `PORTAL_PUBLIC_URL` — внешний origin для коротких ссылок и Origin-проверки.
- `PORTAL_COOKIE_SECURE` — Secure-флаг session cookie; в production оставьте `true`.
- `PORTAL_TRUST_PROXY` — учитывать правый адрес из `X-Forwarded-For`, только
  когда заголовок приходит от непосредственного loopback-прокси; штатной схеме
  не требуется и по умолчанию выключено.
- `PORTAL_ALLOW_PRIVATE_URLS` — разрешает мониторинг loopback/RFC1918. Link-local
  и metadata-адреса остаются заблокированы.
- `PORTAL_ALLOWED_OUTBOUND_PORTS` — разрешённые порты RSS и проверок, обычно
  `80,443`.
- `PORTAL_MONITOR_INTERVAL` и `PORTAL_FEED_REFRESH_INTERVAL` — фоновые интервалы
  в секундах; `0` отключает соответствующую задачу.

Полный bare-metal пример для `/etc/ttx/portal.env` находится в
`portal.env.example`; Compose берёт свои значения из `compose/.env` и
`compose/docker-compose.yml`.

## Границы безопасности

- Все изменяющие API требуют session cookie и CSRF-токен.
- Вход ограничен по частоте, пароль проверяется PBKDF2.
- Сессии привязаны к текущему хешу пароля и сразу перестают действовать после
  его ротации. Для loopback-прокси используется короткий глобальный CPU-бюджет,
  а `X-Forwarded-For` принимается только при `PORTAL_TRUST_PROXY=true` и только
  от непосредственного loopback-пира.
- RSS и monitoring фиксируют проверенный IP на время запроса, повторно проверяют
  каждый redirect и блокируют SSRF/DNS rebinding.
- Публичный API статуса не возвращает URL и тексты внутренних ошибок.
- Короткие ссылки может создавать и удалять только авторизованный пользователь.
- В публичной статике нет названий VPN-компонентов.

## Проверка

```bash
python3 -m unittest discover -s portal/tests -p 'test_*.py'
../tests/port443-smoke.sh
```

Вторая команда выполняется после развёртывания. Для одновременной проверки
сайта и CONNECT задайте `TT_CLIENT_USERNAME` и `TT_CLIENT_PASSWORD`.
