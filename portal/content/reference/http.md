# HTTP — коды и заголовки
<!-- tags: http, web, api, curl, cache -->

Методы, статусы, заголовки и практическая диагностика HTTP-сервисов.

## Семантика методов

| Метод | Назначение | Идемпотентный |
| --- | --- | --- |
| `GET` | получить представление | да |
| `POST` | создать или запустить действие | обычно нет |
| `PUT` | полностью заменить ресурс | да |
| `PATCH` | частично изменить ресурс | зависит от операции |
| `DELETE` | удалить ресурс | да |

## Диапазоны статусов

| Диапазон | Значение |
| --- | --- |
| 2xx | запрос успешно обработан |
| 3xx | перенаправление или работа кэша |
| 4xx | ошибка, конфликт или ограничение клиента |
| 5xx | ошибка сервера или upstream |

Частые коды: `200 OK`, `201 Created`, `204 No Content`, `304 Not Modified`, `400 Bad Request`, `401 Unauthorized`, `403 Forbidden`, `404 Not Found`, `409 Conflict`, `429 Too Many Requests`, `502 Bad Gateway`, `503 Service Unavailable`.

## curl

```sh
curl --fail-with-body --show-error --location https://example.com/
curl --head https://example.com/
curl --write-out '%{http_code} %{time_total}\n' --output /dev/null --silent https://example.com/
curl --request POST --header 'Content-Type: application/json' --data '{"ok":true}' https://example.com/api
```

## Кэш и безопасность

- `Cache-Control: no-store` запрещает хранение ответа.
- `Cache-Control: max-age=300` разрешает считать ответ свежим 5 минут.
- `ETag` и `If-None-Match` дают условный запрос с ответом `304`.
- `Content-Security-Policy` ограничивает источники скриптов, стилей и других ресурсов.
- `Strict-Transport-Security` просит браузер использовать только HTTPS.

> Для чувствительных URL и заголовков не включайте подробный trace в общедоступные логи.
