# EventEdge — подключение Yandex Cloud

Репозиторий подключается к Yandex Cloud через GitHub Actions OIDC и Workload Identity Federation.

## Что это даёт

- GitHub Actions получает короткоживущий IAM-токен только во время job;
- в GitHub нет постоянного JSON-ключа сервисного аккаунта;
- доступ ограничен конкретным репозиторием и веткой `main`;
- роли сервисного аккаунта можно добавлять постепенно под фактические шаги деплоя.

## Создаваемые объекты

| Объект | Имя | Назначение |
|---|---|---|
| Service account | `eventedge-ci` | Идентичность CI/CD |
| Workload Identity Federation | `eventedge-github` | Проверка GitHub OIDC-токенов |
| Federated credential | subject репозитория | Разрешение только `main` конкретного приватного репозитория |
| GitHub Actions variable | `YC_SERVICE_ACCOUNT_ID` | Публичный идентификатор service account, не секрет |

## Проверка

Workflow [yc-connection.yml](../.github/workflows/yc-connection.yml) запускается вручную. Он запрашивает OIDC-токен GitHub, обменивает его на короткоживущий IAM-токен Yandex Cloud и не выводит токены в лог.

Успешная строка:

```text
Yandex Cloud OIDC exchange succeeded
```

## Следующий этап

Сейчас подключение подтверждает безопасную аутентификацию. Перед первым деплоем service account получает только необходимые роли для конкретных ресурсов: Container Registry, Serverless Containers и API Gateway. Роли для YDB, Object Storage, Message Queue и Lockbox добавляются вместе с соответствующим инкрементом, а не заранее.

