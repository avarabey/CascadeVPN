# Docker — контейнеры и Compose
<!-- tags: docker, compose, containers, logs -->

Проверка конфигурации, журналов и аккуратное обновление сервисов.

## Состояние проекта

```sh
docker compose config --quiet
docker compose ps
docker compose images
docker compose top
docker stats --no-stream
```

Сначала запускайте `docker compose config`: команда показывает итоговую конфигурацию после подстановки переменных.

## Журналы и диагностика

```sh
docker compose logs --tail=100 service-name
docker compose logs --since=30m service-name
docker inspect container-name
docker inspect --format '{{json .State.Health}}' container-name
```

## Обновление одного сервиса

```sh
docker compose build service-name
docker compose up -d --no-deps service-name
docker compose ps service-name
docker compose logs --tail=50 service-name
```

## Работа внутри контейнера

```sh
docker compose exec service-name sh
docker compose exec -T service-name command --flag
docker compose cp service-name:/path/to/file ./file
```

| Флаг | Назначение |
| --- | --- |
| `--no-deps` | не пересоздавать зависимости |
| `--force-recreate` | пересоздать даже без изменений |
| `--remove-orphans` | удалить сервисы, исчезнувшие из Compose |

> Перед очисткой образов или volumes проверьте точный список целей: данные в volume могут быть единственной копией.
