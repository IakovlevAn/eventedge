# EventEdge

EventEdge — API-first агрегатор новостей и инструмент поддержки инвестора на
российском рынке. Сервис объединяет публикации в события, извлекает проверяемые
факты, рассчитывает сигналы и сохраняет последующую реакцию рынка.

Production использует rule-based `signal-engine-0.6.1`, `rules-0.3.0` и
`config_version=7`. В репозитории ML-модель значимости упакована в переносимый
JSON и подключена к live pipeline через выключенный по умолчанию
`disabled/shadow/rank` rollout. ML-модель направления не прошла контроли и не
внедряется; production этой веткой не изменён.

## Документация

- [Описание продукта](EventEdge/README.md)
- [ML pipeline: данные, обучение, inference и результаты](EventEdge/ML_PIPELINE.md)
- [Дополнительные эксперименты направления, этапы 1–5](EventEdge/NEWS_DIRECTION_EXPERIMENTS.md)
- [Технический дизайн](EventEdge/TECHNICAL_DESIGN.md)
- [OpenAPI-контракт](EventEdge/openapi.yaml)
- [Подключение Yandex Cloud](EventEdge/CLOUD_SETUP.md)
- [Направление UX/UI](EventEdge/DESIGN_SYSTEM.md)
- [Бюджет и технические лимиты](BUDGET.md)

Машиночитаемые ML-контракты:

- [lineage датасета](EventEdge/DATASET_LINEAGE_CONTRACT.json);
- [locked benchmark](EventEdge/NEWS_MODEL_BENCHMARK_CONTRACT.json);
- [реестр артефактов и метрик](EventEdge/ML_ARTIFACT_MANIFEST.json).

## Локальная проверка

Требуются Python 3.13 и `uv`:

```bash
uv sync --locked --dev
uv run ruff check .
uv run pytest
uv run python scripts/validate_openapi.py
```

Для SELECT-only экспорта YDB дополнительно установите export group:

```bash
uv sync --locked --dev --group export
```

Сетевые collectors и cloud-команды не запускаются тестами автоматически. Они
работают только по явной команде; production не изменяется локальным ML
pipeline.

## Ключевые локальные команды

Проверка и byte-exact пересборка принятого ML checkpoint:

```bash
PYTHONPATH=src:. uv run --locked python scripts/verify_dataset_lineage.py \
  --artifact-root .

PYTHONPATH=src:. uv run --locked python -m scripts.rebuild_canonical_signal_dataset \
  --contract EventEdge/DATASET_LINEAGE_CONTRACT.json \
  --artifact-root . \
  --output-root .local-artifacts/canonical-rebuild
```

Сбор нового Telegram/YDB датасета, MOEX labels, подготовка признаков и
fit/inference обеих переносимых JSON-моделей описаны одной воспроизводимой
цепочкой в [ML_PIPELINE.md](EventEdge/ML_PIPELINE.md). Raw корпуса, свечи,
bundles, модели и predictions намеренно gitignored.

Offline regression gate текущего deterministic signal-router:

```bash
PYTHONPATH=src:. uv run --locked python -m scripts.check_signal_quality_gate \
  --dataset tests/fixtures/retro_signal_audit.json
```

Это синтетический regression contract известных случаев, а не оценка
доходности или human ground truth.

## Локальный UI

```bash
cd web
npm install
npm run dev
```

## Конфигурация

Типизированные defaults находятся в `src/eventedge/configs/`, а локальные
переопределения — в `configs/`:

- `sources.yaml` — RSS и Telegram-источники;
- `collection.yaml` — интервалы и контуры сбора;
- `scoring.yaml` — коэффициенты качества источников.

Другой каталог задаётся через `EVENTEDGE_CONFIG_DIR`. Некорректный YAML
останавливает запуск. Service-account keys, endpoint credentials и локальные
исходные данные нельзя коммитить.
