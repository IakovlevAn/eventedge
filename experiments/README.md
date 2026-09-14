# Пять development-экспериментов EventEdge

Этот каталог хранит исполняемый архив этапов 1–5, проведённых до фиксации
канонического ML-контура в PR #148. Код не входит в production package и не
вызывается приложением, worker или deploy workflow.

## Статус результатов

Эксперименты используют исходный датасет SHA-256
`ac639e944a100c83b91275bbf627d3e9af9c654e5a0ce726dc484c0e2c0b81b6`.
Canonical pipeline использует исправленную сборку SHA-256
`ee47437edd61f2f444f32e1d85e7562fb0a07c47750651217ffc898c55ca632c`.
Метрики этого архива нельзя подменять метриками canonical benchmark без нового
запуска по его dataset, folds и определениям targets.

- этап 1: RuBERT sentiment не дал устойчивого прироста;
- этап 2: пятиминутная abnormal reaction улучшила materiality;
- этап 3: abstention, другие горизонты и neutral-класс не решили direction;
- этап 4: структурированные и TF-IDF признаки не прошли gate;
- этап 5: небольшие нелинейные модели не прошли gate.

Машиночитаемый статус находится в
`EventEdge/NEWS_DIRECTION_EXPERIMENTS_CONTRACT.json`, сводка — в
`EventEdge/NEWS_DIRECTION_EXPERIMENTS.md`.

## Локальные данные

По умолчанию runners ожидают датасет здесь:

```text
.local-data/news-direction-legacy/dataset.jsonl
```

Результаты записываются в:

```text
.local-artifacts/news-direction-legacy/<stage>/
```

Пути можно изменить через `EVENTEDGE_DATASET_PATH` и
`EVENTEDGE_EXPERIMENT_ARTIFACT_ROOT`. Оба каталога исключены из git.

## Среда

Базовые этапы 2–5:

```bash
uv sync --locked --dev --group research
```

Этапы 1 и 2 дополнительно требуют NLP-группу для полного запуска с нуля:

```bash
uv sync --locked --dev --group research --group research-nlp
```

Модель `mxlcw/rubert-tiny2-russian-financial-sentiment` должна загружаться на
зафиксированной revision `a02913e44597582218db7821d52dc15c331bf427`.
CI не скачивает модель и не обращается к MOEX.

## Порядок запуска

После подготовки датасета этапы выполняются последовательно:

```bash
PYTHONPATH=src:. uv run --locked --group research --group research-nlp python \
  experiments/finbert_stage1/run_experiment.py

PYTHONPATH=src:. uv run --locked --group research --group research-nlp python \
  experiments/market_stage2/run_experiment.py

PYTHONPATH=src:. uv run --locked --group research python \
  experiments/direction_stage3/build_label_cache.py \
  --dataset .local-data/news-direction-legacy/dataset.jsonl
PYTHONPATH=src:. uv run --locked --group research python \
  experiments/direction_stage3/run_experiment.py

PYTHONPATH=src:. uv run --locked --group research python \
  experiments/direction_stage4/build_structured_features.py \
  --dataset .local-data/news-direction-legacy/dataset.jsonl
PYTHONPATH=src:. uv run --locked --group research python \
  experiments/direction_stage4/run_experiment.py

PYTHONPATH=src:. uv run --locked --group research python \
  experiments/direction_stage5/run_experiment.py
```

Для byte-exact offline-проверки кэши MOEX должны быть восстановлены в
`.local-artifacts/news-direction-legacy/market_stage2/cache` и
`.local-artifacts/news-direction-legacy/direction_stage3/cache` с SHA-256 из
контракта. Флаг `--offline` запрещает сетевое обновление. Без него MOEX может
вернуть более позднюю архивную версию данных, поэтому новый результат должен
получить отдельный provenance.

`source_metrics_sha256` в контракте фиксирует первоначальные generated JSON до
нормализации локальных путей. Новый metrics-файл может отличаться полем `path`;
сравнивать результаты следует по dataset SHA, protocol и численным метрикам.

## Проверка архива

```bash
PYTHONPATH=src:. uv run --locked python scripts/verify_direction_experiment_archive.py
```

Ноутбуки являются обзором, а Python runners — источником вычислительной логики.
Generated outputs и notebook outputs намеренно не входят в MR.

## Канонический rerun

Адаптер в `canonical_rerun/` повторяет этапы 1–5 на dataset SHA `ee4743…` и
remaining-direction target из PR #148. Он byte-exact восстанавливает канонический
датасет из frozen legacy dataset и MOEX-кэшей, проверяет hashes всех evaluation
folds и контрольный результат config 6, затем запускает общую серию ablations.

Краткий итог находится в
`EventEdge/NEWS_DIRECTION_CANONICAL_RERUN.md`, а машиночитаемые headline-метрики —
в `EventEdge/NEWS_DIRECTION_CANONICAL_RERUN_CONTRACT.json`.
