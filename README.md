# EventEdge

EventEdge — API-first платформа краткосрочных сигналов по российским акциям.

Текущий live-контур сочетает новостной сигнал с детерминированными рыночными
факторами. `GET /v1/assessments` возвращает оценки всего списка наблюдения, а
`GET /v1/evals` — фактическую реакцию цены, сводную статистику и аналитические разрезы.
`GET /v1/evals/export` отдаёт outcomes и event-time временные ряды в CSV или JSON.

- [Понятное описание продукта](EventEdge/README.md)
- [Технический дизайн](EventEdge/TECHNICAL_DESIGN.md)
- [Направление UX/UI](EventEdge/DESIGN_SYSTEM.md)
- [OpenAPI-контракт](EventEdge/openapi.yaml)
- [Подключение Yandex Cloud](EventEdge/CLOUD_SETUP.md)
- [Бюджет и технические лимиты](BUDGET.md)

## Локальная проверка

```bash
uv sync --locked --dev
uv run ruff check .
uv run pytest
uv run python scripts/validate_openapi.py
```

## Offline-проверка качества маршрутизации

Размеченные примеры хранятся в JSONL по схеме `quality-example-1.0`. По
умолчанию evaluator принимает только `label_source=human`: ответы LLM и
синтетические fixtures нельзя незаметно использовать как financial ground
truth. Повторные публикации одного события должны иметь общий `event_id`, чтобы
временное разбиение по `received_at` не разносило их между train, validation и
test. События, пересекающие временную границу, попадают в `purged`, а не в
соседние выборки. `published_at`, `received_at` и `labeled_at` всегда содержат
timezone.

```bash
PYTHONPATH=src uv run python -m scripts.evaluate_quality_dataset \
  --dataset path/to/human-quality-labels.jsonl \
  --include-split
```

Команда только читает локальный файл, сравнивает метки с текущим deterministic
router и печатает воспроизводимый JSON-отчёт. Она не вызывает LLM, не пишет в
YDB и не изменяет production.

CI дополнительно защищает текущий signal-router синтетическим regression
contract:

```bash
PYTHONPATH=src uv run python -m scripts.check_signal_quality_gate \
  --dataset tests/fixtures/retro_signal_audit.json
```

Gate требует 32 примера и precision/recall `1.0`. Это контракт известных
позитивных и негативных случаев, а не оценка качества на реальном рынке и не
human ground truth. Для реальной оценки используется описанный выше JSONL с
`label_source=human`.

## Локальный ML-router перед LLM

Опциональный `ml-router-nb-0.1.0` работает после deterministic signal-router и
до LLM. Он использует только заголовок, первые 2500 символов текста и категории,
доступные на момент обработки новости. Рыночные outcomes, результаты LLM и
будущие данные не входят в признаки. Vocabulary и веса строятся только на
chronological train partition; одинаковые `event_id` не могут попасть в разные
partition.

```bash
PYTHONPATH=src uv run python -m scripts.train_ml_router \
  --dataset path/to/human-quality-labels.jsonl \
  --output path/to/ml-router.json
```

Команда полностью локальная: она не вызывает API, не запускает cloud job и не
пишет в production. По умолчанию принимаются только human labels. Флаг
`--allow-synthetic` предназначен для fixtures; полученный артефакт помечается
`synthetic_test` и запрещён в production.

Runtime по умолчанию выключен (`ML_ROUTER_MODE=disabled`). Для наблюдения без
изменения маршрута задаются `ML_ROUTER_MODE=shadow` и
`ML_ROUTER_ARTIFACT_PATH`. В `enforce` разрешено только уверенное отклонение
нерелевантной новости; accept и abstain продолжают идти в LLM. Startup
останавливается, если артефакт отсутствует, повреждён или не проходит minimum
data/precision/recall/coverage gate. В этом PR production-артефакт и
автоматический backfill не добавляются.

## События и provenance

`GET /v1/events` детерминированно объединяет подтверждающие публикации одного
типа в событие и сохраняет все `news_ids`, источники, evidence и связанные
сигналы. ID образуется от самой ранней известной публикации, поэтому позднее
подтверждение его не меняет; backfill более раннего источника может изменить ID,
пока события не вынесены в отдельное постоянное хранилище. Полная карточка
доступна через `GET /v1/events/{event_id}`. `materiality` и `event_type` здесь —
выход версионированного rule-based extractor, а не будущий market outcome.

## Grounded LLM extraction

`yandexgpt-lite-0.6.0` получает новость как недоверенный JSON-документ и обязан
вернуть 1–3 короткие дословные `evidence_quotes`. Код принимает результат только
тогда, когда каждая цитата найдена в реально переданном модели заголовке или
тексте, а значение каждого структурированного факта присутствует в его
`source_quote`.
Цитаты сохраняются вместе с feature set. При неподтверждённой цитате, подмене
значения, malformed response или сетевой ошибке весь LLM-результат отбрасывается
и используется deterministic rules fallback. Повторного LLM-вызова нет, поэтому
guard не увеличивает число платных запросов. Изменение поведения отделено как
`signal-engine-0.6.1`, `config_version=2`; исторические сигналы не
переписываются и автоматический backfill не запускается.

## Trustworthy evals

Методология `market-outcome-0.2.0` считает только сигналы, которые реально могли
существовать в момент решения. Ретроспективный reprocess, evidence из будущего и
несогласованные `as_of`/`data_cutoff_at` получают `status=excluded` и не входят в
hit rate. Entry берётся после фактического `created_at`, а горизонт засчитывается
только при наличии свечи не позднее 20 минут от целевого времени. Outcomes и
выгрузки сохраняют decision time, фактическое время наблюдения и задержки.
Комиссии, проскальзывание и поправки на корпоративные действия пока не учтены,
поэтому eval не является доказательством доходности.

## Локальный UI

```bash
cd web
npm install
npm run dev
```

## Конфигурация

Безопасные значения по умолчанию описаны типизированными `dataclass` в
`src/eventedge/configs/`. Опциональные переопределения находятся в корневом
каталоге `configs/`:

- `sources.yaml` — параметры RSS-источников и публичных Telegram-каналов;
- `collection.yaml` — интервалы, состав контуров и timer triggers;
- `scoring.yaml` — коэффициенты качества источников.

Поля, отсутствующие в YAML, сохраняют значения по умолчанию. Другой каталог
можно указать через `EVENTEDGE_CONFIG_DIR`; некорректный YAML останавливает
запуск приложения с ошибкой конфигурации.
