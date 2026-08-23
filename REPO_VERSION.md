# REPO version

**Version:** `0.2.4`

**Specification:** [SPEC.md](SPEC.md)

**Specification revision:** `2026-08-23`

**Release state:** production accepted on 2026-08-23.

Эта версия сохраняет neutral portal ffknd 0.1.1, Nginx SNI router,
Xray Reality backend на loopback, независимый TrustTunnel на `8443/tcp+udp`
и публичную подписку 3x-ui на `65535/tcp`. Она восстанавливает автоматическое
обновление отдельного short-lived IP-сертификата подписки по
[SPEC.md §14.6](SPEC.md#146-обновление-короткоживущего-ip-сертификата-подписки).

`acme.sh` переведён со standalone на Nginx webroot `/var/www/html`, новый
сертификат установлен и принят внешним TLS/HTTP smoke, cron сохранил следующий
renew. После reload x-ui повторно прошли VLESS Reality на `443`, TrustTunnel
e2e, portal health и подписка. Reality target и клиенты не менялись.
