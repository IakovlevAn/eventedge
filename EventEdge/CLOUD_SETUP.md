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
│   ├── Serverless Container: eventedge-worker
│   ├── API Gateway: eventedge-api
│   ├── YDB Serverless: eventedge-prod
│   ├── Timer trigger: eventedge-fast-news
│   ├── Timer trigger: eventedge-discovery-news
│   ├── Timer trigger: eventedge-slow-news
│   ├── Timer trigger: eventedge-maintenance
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
| Serverless Container | `eventedge-api` | Публичный read API: один прогретый инстанс на активную зону, до семи параллельных запросов |
| Serverless Container | `eventedge-worker` | Сбор новостей, reprocess и Evals без прогретых инстансов |
| Service account | `eventedge-gateway` | Вызов только приватного контейнера API |
| API Gateway | `eventedge-api` | Публичная точка входа и маршрутизация к контейнеру |
| YDB Serverless | `eventedge-prod` | Новости, признаки, сигналы, канонические assessment-snapshot, idempotency-записи и jobs |
| Timer trigger | `eventedge-fast-news` | Интерфакс, ТАСС, РБК и Московская биржа каждую минуту |
| Timer trigger | `eventedge-discovery-news` | Google News и добавленные через UI Telegram каждые 5 минут |
| Timer trigger | `eventedge-slow-news` | Макроэкономический фон Банка России каждые 15 минут |
| Timer trigger | `eventedge-maintenance` | Пересчёт сохранённого Evals-snapshot и reprocess кандидатов каждые 10 минут |

## CI/CD и автоматические merge

- `CI` запускает lint, тесты, проверку OpenAPI, сборку Python-пакета и Docker-образа.
- Первый bootstrap PR мержится вручную после зелёного CI.
- После bootstrap trusted workflow автоматически делает squash merge зелёных PR из веток `agent/*` в `main`.
- Workflow сверяет SHA проверенной ревизии, репозиторий ветки и target `main`; draft и PR с label `do-not-merge` не мержатся.
- После зелёного CI на `main` выполняется OIDC-аутентификация, публикация образа и деплой новой ревизии.
- API-ревизия использует 2 vCPU / 4 GB, concurrency 7, `min-instances=1` и лимит одного инстанса на зону. Короткоживущие assessment-snapshot канонизируются транзакцией в YDB, поэтому согласованность не зависит от process-local кеша. Worker использует те же ресурсы по требованию, но обрабатывает только одну timer-задачу одновременно и имеет `min-instances=0`.
- YDB не имеет зарезервированной мощности, ограничена 10 RU/с и 1 ГБ, защищена от удаления.
- Перед публикацией serverless-ревизии CI применяет DDL из того же Docker-образа. Для этого `eventedge-ci` должен иметь `ydb.editor` только на базе `eventedge-prod`.
- Runtime service account имеет `ydb.editor` только на базе `eventedge-prod`; роль не выдана на каталог или облако.
- Runtime service account имеет `ai.languageModels.user` в production-каталоге для вызова YandexGPT через короткоживущий metadata IAM token.
- Все четыре Timer управляются тем же проверенным deploy через [`scripts/deploy_triggers.py`](../scripts/deploy_triggers.py). CI имеет `functions.editor` только в production-каталоге проекта.

GitHub Free не предоставляет branch protection для приватного репозитория. Поэтому запрет прямого push в `main` нельзя обеспечить на стороне GitHub без GitHub Pro; автоматический pipeline сам прямой push не использует.

## Production endpoint

```text
https://d5d8smlpd6q241aquti1.kocrdvxt.apigw.yandexcloud.net
```

Gateway и YDB-backed ревизия контейнера развёрнуты. Полная цепочка `CI → OIDC → image → revision → YDB readiness → SHA guard → web smoke test` подтверждена успешным [production deployment](https://github.com/IakovlevAn/eventedge/actions/runs/31266558879) 8 августа 2026 года.

Новостной ingestion пока не опубликован в API Gateway. `POST /v1/internal/news` доступен только через приватный URL контейнера с IAM-аутентификацией. Публичный маршрут появится вместе с отдельным ключом ingestor в Lockbox; до этого случайно открыть служебную загрузку наружу нельзя.

Приватный production smoke test подтвердил запись синтетической новости в `eventedge-prod`, повтор запроса с тем же `Idempotency-Key` без дубля и последующее чтение созданного job. DDL выполняется один раз отдельным шагом деплоя с ограниченным exponential backoff при лимите schema operations; serverless-инстансы при старте только подключаются к готовой схеме и валидируют YDB-запросы в режиме `EXPLAIN`.

Приоритетный контур собирает прямые RSS и шесть отобранных Telegram-каналов каждую минуту. Google News и Telegram-каналы, добавленные через UI, обновляются каждые 5 минут, Банк России — каждые 15 минут. Поэтому рост пользовательского реестра не увеличивает минутную волну с 6 до 18 каналов. High-recall фильтр оставляет экономически релевантные события, включая рынок и отрасли без прямого тикера; только они отправляются в YandexGPT, после чего версия `signal-engine-0.6.1` отсекает сводки уже случившегося движения и детерминированно выбирает target, направление и действие.

Публичный `/v1/evals` не ходит в MOEX и ничего не пересчитывает: он читает последний snapshot из YDB. Тяжёлый пересчёт запускается в worker каждые 10 минут, включает только направленные `up/down`-сигналы, сохраняет сырые временные ряды и отдельные эпохи моделей. Нейтральные новости остаются в истории, но не входят в hit rate и Pearson.

## Проверка

Workflow [yc-connection.yml](../.github/workflows/yc-connection.yml) запускается вручную. Он запрашивает OIDC-токен GitHub, обменивает его на короткоживущий IAM-токен Yandex Cloud и не выводит токены в лог.

Успешная строка:

```text
Yandex Cloud OIDC exchange succeeded
```

## Следующий этап

Следующий этап — накопить 72 часа метрик раздельных API/worker-контуров, затем решить, можно ли безопасно уменьшить API до 512 MB. Object Storage нужен для будущих тяжёлых архивов и экспортов; Message Queue не подключается, пока последовательного worker и timer-очереди достаточно.
