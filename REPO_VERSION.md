# REPO version

**Version:** `0.2.5`

**Specification:** [SPEC.md](SPEC.md)

**Specification revision:** `2026-10-16`

**Release state:** production accepted on 2026-10-16.

Эта версия сохраняет neutral portal ffknd 0.1.1, Nginx SNI router,
Xray Reality backend на loopback, независимый TrustTunnel на `8443/tcp+udp`
и публичную подписку 3x-ui на `65535/tcp`. После web update 3x-ui она
автоматически возвращает TrustTunnel и bridge, точечно восстанавливает прежний
subscription URL и синхронизирует учётные данные польского outbound с текущим
клиентом по [SPEC.md §14.7](SPEC.md#147-восстановление-после-web-update-3x-ui).

Приняты обычная TLS-проверка subscription endpoint, TrustTunnel e2e с выходом
через Польшу, настоящий VLESS Reality smoke на `443`, `ttx doctor` и dry-run
reconcile. Изменения защищены online SQLite backup и root-only rollback-state.
