# Linux — быстрая диагностика
<!-- tags: linux, shell, systemd, operations -->

Команды для первичной проверки хоста без изменения его состояния.

## Общая картина

```sh
uptime
df -h
free -h
systemctl --failed
journalctl -p warning..alert --since today
```

## Процессы и ресурсы

```sh
ps aux --sort=-%cpu | head
ps aux --sort=-%mem | head
top -o %CPU
du -xhd1 /var | sort -h
```

## Сокеты и сеть

```sh
ss -lntup
ip -brief address
ip route
getent hosts example.com
curl --fail --show-error --head https://example.com/
```

## systemd и журналы

```sh
systemctl status service-name --no-pager
journalctl -u service-name --since '-30 min' --no-pager
journalctl -u service-name -f
```

| Сигнал | Обычное действие |
| --- | --- |
| `TERM` | корректно завершить процесс |
| `HUP` | перечитать конфигурацию, если поддерживается |
| `KILL` | немедленно остановить; использовать в крайнем случае |

> Не публикуйте вывод окружения процессов: там могут находиться токены и пароли.
