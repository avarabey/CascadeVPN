# REPO version

**Version:** `0.2.0`

**Specification:** [SPEC.md](SPEC.md)

**Specification revision:** `2026-08-20`

**Release state:** production accepted on 2026-08-20.

Эта версия описывает neutral portal ffknd, Nginx SNI router, Xray Reality
backend на loopback и независимый TrustTunnel на `8443/tcp+udp`.

Все критерии [SPEC.md §14.3](SPEC.md#143-production-приёмка-ffkndru)
выполнены, включая реальный VLESS Reality smoke после restart Xray,
проверку публичного TLS и SQLite rollback backup.
