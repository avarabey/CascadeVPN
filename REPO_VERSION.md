# REPO version

**Version:** `0.2.2`

**Specification:** [SPEC.md](SPEC.md)

**Specification revision:** `2026-08-21`

**Release state:** production accepted on 2026-08-21.

Эта версия описывает neutral portal ffknd 0.1.1, Nginx SNI router,
Xray Reality backend на loopback, независимый TrustTunnel на `8443/tcp+udp`
и memory guard для 1 GiB production VM по
[SPEC.md §14.4](SPEC.md#144-защита-production-хоста-от-исчерпания-памяти).

Все критерии [SPEC.md §14.3](SPEC.md#143-production-приёмка-ffkndru) и
[SPEC.md §14.4](SPEC.md#144-защита-production-хоста-от-исчерпания-памяти)
выполнены. После memory hardening повторно прошли официальный
TrustTunnel Client e2e на `8443`, VLESS Reality e2e на `443`, public TLS,
portal health и полная listener-карта.
