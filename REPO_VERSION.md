# REPO version

**Version:** `0.2.3`

**Specification:** [SPEC.md](SPEC.md)

**Specification revision:** `2026-08-23`

**Release state:** production accepted on 2026-08-23.

Эта версия сохраняет neutral portal ffknd 0.1.1, Nginx SNI router,
Xray Reality backend на loopback, независимый TrustTunnel на `8443/tcp+udp`
и memory guard для 1 GiB production VM. Дополнительно она фиксирует рабочий
публичный HTTPS endpoint подписок 3x-ui на `65535/tcp` по
[SPEC.md §14.5](SPEC.md#145-публичная-3x-ui-подписка).

Критерии [SPEC.md §14.5](SPEC.md#145-публичная-3x-ui-подписка) выполнены:
конфликтующий UFW `REJECT 65535/tcp` удалён, внешний и локальный запросы
подписки вернули `200 OK`, TLS-проверка по IP прошла, сервисы и listener-карта
не изменились. Предыдущая приёмка §14.3 и §14.4 остаётся действующей.
