# Runbook

## Клиенты подключаются, но интернета нет

1. `ttx status` — есть ли ingress и указывает ли на него vpn.toml.
2. `ss -ltnp | grep 10800` — слушает ли xray. Если нет: inbound выключен в панели
   или xray не стартовал (журнал панели → Xray Status).
3. `curl --socks5-hostname 127.0.0.1:10800 https://ifconfig.me` — работает ли
   цепочка после моста. Если нет — проблема в routing/outbound 3x-ui, туннель ни при чём.
4. Если п.3 работает, а у клиента нет — смотрите `journalctl -u trusttunnel -f`.

## Не работает QUIC / медленный DNS у клиентов
Проверьте `udp: true` у ingress-inbound и поддержку UDP ASSOCIATE в вашей версии
endpoint'а. Временный обход: включить у клиента DNS-upstream по DoT/DoH через TCP.

## После обновления 3x-ui пропал inbound
Мост восстановит его в течение `interval_secs`. Ускорить: `ttx reconcile`.

## После обновления TrustTunnel сервис не стартует
```bash
ttx doctor                    # покажет несоответствие версии compat.json
systemctl status trusttunnel
```
Откат: `cp /opt/trusttunnel/vpn.toml.ttx-bak /opt/trusttunnel/vpn.toml`
и установка предыдущей версии через `install.sh -V <version>`.

## Полное отключение обвязки (возврат к «чистому» TrustTunnel)
```bash
systemctl disable --now ttx-bridge ttx-reconcile.timer
rm /etc/systemd/system/trusttunnel.service.d/10-ttx-overlay.conf
systemctl daemon-reload
# убрать управляемую секцию из конфига:
sed -i '/managed by ttx-bridge/,$d' /opt/trusttunnel/vpn.toml
systemctl restart trusttunnel
```
Оба upstream-проекта остаются полностью работоспособными: обвязка ничего в них
не меняла.
