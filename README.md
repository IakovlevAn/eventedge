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
