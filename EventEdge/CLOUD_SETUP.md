# EventEdge — подключение Yandex Cloud

Репозиторий подключается к Yandex Cloud через GitHub Actions OIDC и Workload Identity Federation.

## Облачное окружение

```text
cloud: eventedge
├── folder: prod
│   ├── service account: eventedge-ci
│   ├── service account: eventedge-api
│   ├── service account: eventedge-gateway
│   ├── federation: eventedge-github
│   ├── Container Registry: eventedge
│   ├── Serverless Container: eventedge-api
│   ├── API Gateway: eventedge-api
│   ├── YDB Serverless: eventedge-prod
│   ├── Timer trigger: eventedge-cbr-press
│   └── VPC: eventedge-prod (без подсетей и разрешающих правил)
└── folder: dev
```

Старые функции, триггеры, service accounts, подсети и логи из облака удалены. Production VPC пока оставлена без подсетей и правил: необходимые сетевые ресурсы будут создаваться вместе с инфраструктурным кодом.

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
| GitHub Actions variable | `YC_CLOUD_ID` | Идентификатор облака `eventedge`, не секрет |
| GitHub Actions variable | `YC_FOLDER_ID` | Идентификатор production-каталога, не секрет |
| GitHub Actions variable | `YC_DEV_FOLDER_ID` | Идентификатор development-каталога, не секрет |
| Container Registry | `eventedge` | Неизменяемые Docker-образы API |
| Service account | `eventedge-api` | Скачивание production-образа и runtime-идентичность API |
| Serverless Container | `eventedge-api` | Production API с масштабированием до нуля |
| Service account | `eventedge-gateway` | Вызов только приватного контейнера API |
| API Gateway | `eventedge-api` | Публичная точка входа и маршрутизация к контейнеру |
| YDB Serverless | `eventedge-prod` | Новости, idempotency-записи и асинхронные jobs |
| Timer trigger | `eventedge-cbr-press` | Опрос официального RSS Банка России раз в 15 минут |

## CI/CD и автоматические merge

- `CI` запускает lint, тесты, проверку OpenAPI, сборку Python-пакета и Docker-образа.
- Первый bootstrap PR мержится вручную после зелёного CI.
- После bootstrap trusted workflow автоматически делает squash merge зелёных PR из веток `agent/*` в `main`.
- Workflow сверяет SHA проверенной ревизии, репозиторий ветки и target `main`; draft и PR с label `do-not-merge` не мержатся.
- После зелёного CI на `main` выполняется OIDC-аутентификация, публикация образа и деплой новой ревизии.
- Production-ревизия ограничена 256 MB памяти, одной инстанцией на зону, 50 запросами на зону и `min-instances=0`.
- YDB не имеет зарезервированной мощности, ограничена 10 RU/с и 1 ГБ, защищена от удаления.
- Runtime service account имеет `ydb.editor` только на базе `eventedge-prod`; роль не выдана на каталог или облако.

GitHub Free не предоставляет branch protection для приватного репозитория. Поэтому запрет прямого push в `main` нельзя обеспечить на стороне GitHub без GitHub Pro; автоматический pipeline сам прямой push не использует.

## Production endpoint

```text
https://d5d8smlpd6q241aquti1.kocrdvxt.apigw.yandexcloud.net
```

Gateway и YDB-backed ревизия контейнера развёрнуты. Полная цепочка `CI → OIDC → image → revision → YDB readiness → SHA guard → web smoke test` подтверждена успешным [production deployment](https://github.com/IakovlevAn/eventedge/actions/runs/31266558879) 8 августа 2026 года.

Новостной ingestion пока не опубликован в API Gateway. `POST /v1/internal/news` доступен только через приватный URL контейнера с IAM-аутентификацией. Публичный маршрут появится вместе с отдельным ключом ingestor в Lockbox; до этого случайно открыть служебную загрузку наружу нельзя.

Приватный production smoke test подтвердил запись синтетической новости в `eventedge-prod`, повтор запроса с тем же `Idempotency-Key` без дубля и последующее чтение созданного job.

Первый реальный источник — [официальный RSS пресс-релизов Банка России](https://www.cbr.ru/rss/RssPress). Production poll загрузил 10 публикаций; повторный poll вернул 10 replay и не создал дублей. Trigger `eventedge-cbr-press` активен с расписанием `0,15,30,45 * ? * * *`, использует существующий `eventedge-gateway` и делает до трёх попыток с интервалом 30 секунд.

## Проверка

Workflow [yc-connection.yml](../.github/workflows/yc-connection.yml) запускается вручную. Он запрашивает OIDC-токен GitHub, обменивает его на короткоживущий IAM-токен Yandex Cloud и не выводит токены в лог.

Успешная строка:

```text
Yandex Cloud OIDC exchange succeeded
```

## Следующий этап

Следующий этап — извлекать из сохранённых новостей структурированные семантические признаки и рассчитывать первую версию scoring. Роль для Yandex Foundation Models будет запрошена отдельно и только перед реальным LLM-вызовом; роли для Object Storage и Message Queue добавляются вместе с использующим их инкрементом, а не заранее.
