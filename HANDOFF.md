# Handoff: ffknd.ru portal production

Актуальная спецификация: [SPEC.md](SPEC.md). Версия REPO:
[0.2.1](REPO_VERSION.md).

## Текущее подтверждённое состояние

- Host: `root@135.106.175.165`; не отключать проверку SSH host key.
- Nginx — единственный владелец `0.0.0.0:443/tcp` и делит трафик по SNI.
- `ffknd.ru` и `www.ffknd.ru` идут через TLS terminator
  `127.0.0.1:9443` в portal origin `127.0.0.1:8080`.
- Любой другой или пустой SNI идёт без TLS termination и PROXY protocol в
  Xray VLESS Reality `127.0.0.1:10443`.
- Inbound 3x-ui `id=6`, текущий tag `in-10443-tcp`; клиентам по-прежнему
  рекламируется `ffknd.ru:443`, Reality SNI — `cloud.ru`.
- TrustTunnel независимо слушает `8443/tcp+udp` и использует Xray SOCKS
  ingress `127.0.0.1:10800`.
- `x-ui`, `nginx`, `ffknd-portal`, `trusttunnel`, `ttx-bridge` active.
- Portal 0.1.1 развёрнут; QR SVG сбрасывает глобальную icon-stroke
  стилизацию, а CSS/главный JS загружаются с version cache-buster.
- Let's Encrypt сертификат `ffknd.ru` + `www.ffknd.ru` действует до
  `2026-11-18`; deploy hook проверяет конфиг и reload'ит Nginx.
- `https://ffknd.ru/api/health`, redirect `www`, default-SNI TLS и реальный
  VLESS Reality smoke прошли после явного restart Xray.

## Состояние и резервные копии

- Root-only состояние cutover: `/var/lib/ffknd-xui-cutover/`.
- Online backup x-ui перед cutover:
  `/var/lib/ffknd-xui-cutover/x-ui-before-20260820T221038Z.db`; SQLite
  `quick_check` вернул `ok`.
- Снимок конфигураций до cutover:
  `/root/ffknd-cutover-backup-20260820T0700Z`.
- Инструменты live cutover: `/root/ffknd-cutover-tools`.
- SHA-256 `deploy/xui_portal_cutover.py` для версии 0.2.0:
  `843a0c19b6ae631eec6dd4343cb44482a618ac6bdecd88c659984fc2150b11c6`.
- Старый TrustTunnel drop-in сохранён в
  `/etc/systemd/system/trusttunnel.service.d/10-ttx-overlay.conf.before-portal-v0.2.0`.
- Файлы portal до QR bugfix сохранены в
  `/root/ffknd-portal-qr-before-v0.2.1`.
- Начальный пароль портала не выводить в логи/чат; он лежит root-only в
  `/root/ffknd-portal-initial-password`.

## Важное исправление после cutover

Старый live drop-in TrustTunnel содержал `ExecStartPre=ttx reconcile`. При
restart x-ui он один раз попал в короткое окно, когда API панели ещё не
отвечал; systemd восстановил TrustTunnel через три секунды. Drop-in заменён
версией из репозитория, содержащей только зависимости запуска, и выполнен
`systemctl daemon-reload` без перезапуска VPN. Не возвращать `ExecStartPre`:
изменения применяет транзакционный `ttx-bridge` с health check и rollback.

## Быстрая проверка

```bash
systemctl is-active x-ui nginx ffknd-portal trusttunnel ttx-bridge
ss -lntup | grep -E ':(443|8443|8080|9443|10443|10800)\b'
curl --fail http://127.0.0.1:8080/api/health
python3 /root/ffknd-cutover-tools/xui_portal_cutover.py status
env REALITY_SMOKE_INBOUND_TAG=in-10443-tcp \
  bash /root/ffknd-cutover-tools/reality-e2e-smoke.sh
```

Снаружи дополнительно проверить `https://ffknd.ru/api/health` без `-k` и
фиксированный `308` с `https://www.ffknd.ru/...` на apex.

## Аварийный rollback production 443

Полный rollback обрывает текущие stream-соединения. Сначала освободить `443`
от Nginx, затем вернуть inbound через root-only state — обратный порядок даст
конфликт bind:

```bash
systemctl stop nginx
python3 /root/ffknd-cutover-tools/xui_portal_cutover.py rollback
ss -lntp | grep ':443\b'
systemctl is-active x-ui trusttunnel ttx-bridge
```

После rollback не запускать production stream-конфигурацию, пока Xray снова
владеет публичным `443`. Для возврата к production сначала нужен новый dry-run
и контролируемый `live_portal_cutover.sh`.

Нельзя запускать generic `deploy/deploy.sh`, installer или Compose на этом
сервере: они описывают reference-топологию, где TrustTunnel владеет `443`, и
могут затронуть рабочие Xray/TrustTunnel настройки. Не менять UFW или публичный
доступ 3x-ui на `24527` без отдельного разрешения пользователя.
