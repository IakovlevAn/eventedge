# ML pipeline: данные, обучение и результаты

Этот документ описывает поддерживаемый локальный путь от исходных данных до
двух переносимых JSON-моделей EventEdge. Production и удалённая YDB этим
контуром не изменяются. Крупные входы, подготовленные bundles, модели и
predictions хранятся в gitignored `.local-data/` и `.local-artifacts/`; в Git
остаются код, контракты, агрегированные метрики и SHA-256.

## Что сохранено

```text
immutable Telegram archive ─┐
                            ├─> homogeneous candidates v1/v2
read-only YDB export ───────┘             │
                                          ├─> issuer + IMOEX2 minute opens
MOEX ISS minute opens ────────────────────┘             │
                                                        v
                                               labeled dataset v1/v2
                                                        │
                                             causal prepare at +5 min
                                                        │
                                    ┌───────────────────┴──────────────────┐
                                    v                                      v
                         materiality JSON model                 direction JSON model
                         retained for shadow                    research only / no-go
```

Каждая команда пишет в новый путь и завершается с ошибкой при schema/hash
mismatch, смешении v1/v2 или конфликтующих свечах. Совпадающие дубликаты одной
свечи при подготовке признаков схлопываются; разные `open` для одного
`ticker+timestamp` отклоняются независимо от порядка файлов.

## Fresh dataset из сырых источников

### 1. Telegram или YDB

Telegram downloader разрешён только для каналов, чьё использование явно
согласовано. Даты задаются в UTC (`since` включается, `until` не включается):

```bash
PYTHONPATH=src:. uv run --locked python scripts/download_telegram_archive.py \
  --all-configured-channels \
  --output-dir .local-data/telegram-archive \
  --since 2026-01-01 --until 2026-09-08 \
  --confirm-content-permission \
  --consent-reference CONSENT-REFERENCE \
  --max-run-seconds 3600

PYTHONPATH=src:. uv run --locked python scripts/build_telegram_archive_candidates.py \
  --manifest .local-data/telegram-archive/manifest.json \
  --artifact-root . \
  --output .local-artifacts/fresh-telegram/candidates-v2.jsonl \
  --report .local-artifacts/fresh-telegram/candidates-report.json \
  --assumed-receipt-lag-minutes 5 \
  --confirm-content-permission
```

Downloader имеет лимиты, wall-clock timeout, bounded retries, crash-safe
checkpoint и `--resume`. Raw archive остаётся неизменяемым входом: его не надо
«переносить в v2». Candidate builder создаёт новую производную
`signal-event-candidate-2.0` с `event_group_id`, точным хэшем анализированного
текста и явным provenance. Изменённые посты остаются в архиве, но не попадают в
candidates. Рекомендуемый `--manifest` проверяет `complete`, consent, окно,
канал, число строк и SHA каждого файла. Прямой `--archive` оставлен только для
локальной диагностики и помечается в отчёте как `direct_unsealed_files`.

YDB export требует отдельной dependency group и сервисного аккаунта только с
`SELECT` на разрешённые таблицы:

```bash
uv sync --locked --dev --group export

PYTHONPATH=src:. uv run --locked python scripts/export_ydb_signal_data.py \
  --connection-string '<grpcs-endpoint-with-database>' \
  --service-account-key-file /absolute/path/to/yc-sa-key.json \
  --output-dir .local-data/ydb-export

PYTHONPATH=src:. uv run --locked python scripts/build_ydb_signal_candidates.py \
  --export-dir .local-data/ydb-export \
  --output .local-artifacts/fresh-ydb/candidates-v1.jsonl \
  --report .local-artifacts/fresh-ydb/candidates-report.json \
  --use-stored-feature-sets \
  --maximum-decision-delay-seconds 300
```

YDB builder создаёт `signal-event-candidate-1.0`. Он рассматривает только
эмитентов, уже перечисленных в `source_metadata.tickers`; это eligibility gate,
а не повторное извлечение всех возможных связей из raw-текста. Telegram из YDB
по умолчанию исключён и требует отдельного `--include-telegram-with-permission`.
Admission по умолчанию также исключает строки, у которых итоговый
`decision_at` оказался позже `publication + 5m`; это тот же causal cutoff,
который требует fresh model prepare. Не смешивайте v1 и v2 в одном
candidate/dataset-файле.

### 2. Минутные opens и labels

Сначала скачайте opens инструментов, встречающихся в candidates, и IMOEX2 на
том же интервале:

```bash
PYTHONPATH=src:. uv run --locked python scripts/download_moex_market_opens.py \
  --candidates .local-artifacts/fresh-telegram/candidates-v2.jsonl \
  --from-date 2026-01-01 --till-date 2026-09-08 \
  --interval-minutes 1 \
  --maximum-rows-per-ticker 500000 \
  --output .local-data/moex/issuer-opens.jsonl

PYTHONPATH=src:. uv run --locked python scripts/download_moex_index_opens.py \
  --index-id IMOEX2 \
  --from-date 2026-01-01 --till-date 2026-09-08 \
  --interval-minutes 1 \
  --maximum-rows-per-index 500000 \
  --output .local-data/moex/imoex2-opens.jsonl

PYTHONPATH=src:. uv run --locked python scripts/build_signal_dataset.py \
  --candidates .local-artifacts/fresh-telegram/candidates-v2.jsonl \
  --market-opens .local-data/moex/issuer-opens.jsonl \
  --benchmark-opens .local-data/moex/imoex2-opens.jsonl \
  --benchmark-id IMOEX2 \
  --maximum-entry-lag-seconds 60 \
  --from-decision-date 2026-01-01 \
  --till-decision-date 2026-09-08 \
  --output .local-artifacts/fresh-telegram/dataset-v2.jsonl \
  --report .local-artifacts/fresh-telegram/dataset-report.json
```

Для v1 candidates получается `signal-dataset-example-1.0`, для v2 —
`signal-dataset-example-2.0`. Entry — первый open строго после decision, не
позже 60 секунд; target — open через четыре часа с допустимой задержкой не
более 20 минут. `label_available_at` наступает после обеих target-свечей акции и
benchmark. Raw, benchmark и abnormal returns сохраняются отдельно. Поправки на
корпоративные действия не реконструируются: неизвестный статус остаётся
`unknown`, а не превращается в ложное `none`.

Один MOEX download ограничен 370 календарными днями. Для более длинной
истории скачивайте непересекающиеся chunks в отдельные immutable файлы;
issuer-файлы можно передать повторяющимися `--market-opens`/`--stock-opens`.
Benchmark для одной сборки должен быть заранее объединён в один проверенный
файл без конфликтующих `timestamp/open`.

### 3. Causal prepare, fit и inference

Подготовка использует только признаки, доступные к `publication + 5 minutes`,
держит `event_group_id` целиком и применяет 72-часовой embargo:

```bash
PYTHONPATH=src:. uv run --locked python scripts/prepare_news_model_dataset.py \
  --dataset .local-artifacts/fresh-telegram/dataset-v2.jsonl \
  --stock-opens .local-data/moex/issuer-opens.jsonl \
  --benchmark-opens .local-data/moex/imoex2-opens.jsonl \
  --benchmark-id IMOEX2 \
  --validation-from 2026-05-01T00:00:00+03:00 \
  --evaluation-from 2026-07-01T00:00:00+03:00 \
  --evaluation-until 2026-09-01T00:00:00+03:00 \
  --output .local-artifacts/fresh-telegram/prepared
```

Команда печатает SHA-256 `fit-bundle.json`. Передайте его буквально в обе
команды fit; fit не читает evaluation outcomes. Команды создают строгий JSON,
а не executable `pickle`:

```bash
PYTHONPATH=src:. uv run --locked python scripts/fit_news_materiality_model.py \
  --fit-bundle .local-artifacts/fresh-telegram/prepared/fit-bundle.json \
  --expected-fit-bundle-sha256 '<fit_bundle_sha256>' \
  --output .local-artifacts/fresh-telegram/materiality-model.json

PYTHONPATH=src:. uv run --locked python scripts/fit_news_direction_model.py \
  --fit-bundle .local-artifacts/fresh-telegram/prepared/fit-bundle.json \
  --expected-fit-bundle-sha256 '<fit_bundle_sha256>' \
  --output .local-artifacts/fresh-telegram/direction-model.json

PYTHONPATH=src:. uv run --locked python scripts/predict_news_materiality.py \
  --artifact .local-artifacts/fresh-telegram/materiality-model.json \
  --expected-payload-sha256 '<materiality_payload_sha256>' \
  --input .local-artifacts/fresh-telegram/prepared/update-5m-inputs.jsonl \
  --output .local-artifacts/fresh-telegram/materiality-predictions.jsonl

PYTHONPATH=src:. uv run --locked python scripts/predict_news_direction.py \
  --artifact .local-artifacts/fresh-telegram/direction-model.json \
  --expected-payload-sha256 '<direction_payload_sha256>' \
  --input .local-artifacts/fresh-telegram/prepared/update-5m-inputs.jsonl \
  --output .local-artifacts/fresh-telegram/direction-predictions.jsonl
```

Fresh generic bundle фиксирует directional contract
`update_5m_common_direction`; для него пока нет locked quality claim. Он не
равнозначен каноническому кандидату с исправленными semantic features.

## Канонический benchmark на 1 238 строках

Historical research proxy `canonical_v4_point_in_time_1238` содержит 1 238
строк, 1 226 событий и 1 108 information groups за 2024-09-16—2026-09-04. Все
labels используют IMOEX2. Dataset SHA-256:
`ee47437edd61f2f444f32e1d85e7562fb0a07c47750651217ffc898c55ca632c`.

Byte-exact lineage начинается с принятого checkpoint на 592 строки, а не с
сетевого raw/LLM шага. Требуемые gitignored файлы и их хэши перечислены в
[`DATASET_LINEAGE_CONTRACT.json`](DATASET_LINEAGE_CONTRACT.json) и
[`NEWS_MODEL_BENCHMARK_CONTRACT.json`](NEWS_MODEL_BENCHMARK_CONTRACT.json):

```bash
PYTHONPATH=src:. uv run --locked python scripts/verify_dataset_lineage.py \
  --artifact-root .

PYTHONPATH=src:. uv run --locked python -m scripts.rebuild_canonical_signal_dataset \
  --contract EventEdge/DATASET_LINEAGE_CONTRACT.json \
  --artifact-root . \
  --output-root .local-artifacts/canonical-rebuild

PYTHONPATH=src:. uv run --locked python scripts/prepare_news_model_data.py \
  --contract EventEdge/NEWS_MODEL_BENCHMARK_CONTRACT.json \
  --artifact-root . \
  --output .local-artifacts/news-model-prepared

PYTHONPATH=src:. uv run --locked python scripts/evaluate_news_models.py fit \
  --fit-bundle .local-artifacts/news-model-prepared/fit-bundle.json \
  --expected-fit-bundle-sha256 '<fit_bundle_sha256>' \
  --output .local-artifacts/news-model-run

PYTHONPATH=src:. uv run --locked python scripts/evaluate_news_models.py score \
  --output .local-artifacts/news-model-run \
  --outcomes .local-artifacts/news-model-prepared/outcomes.jsonl \
  --scoring-seal .local-artifacts/news-model-prepared/scoring-seal.json \
  --contract EventEdge/NEWS_MODEL_BENCHMARK_CONTRACT.json \
  --outcomes-sha256 '<outcomes_sha256>'
```

На 480 одинаковых chronological walk-forward rows с group-aware weighting и
72-часовым embargo получены следующие development-метрики:

| Задача | Кандидат | Результат | Решение |
| --- | --- | --- | --- |
| Значимость: `abs(abnormal 4h) >= 0,5 п.п.` | `update_5m_symmetric_materiality_logistic` | raw ROC-AUC `0,7050`, raw PR-AUC `0,6817`, calibrated AUC `0,6905`, Brier `0,2228`; precision `68,81%` при coverage `22,71%` | Shadow candidate |
| Направление оставшегося abnormal return после +5m | `update_5m_corrected_reaction_interactions` | ROC-AUC `0,4982`, hit rate `49,58%`; market-only `50,63%`, always-down `53,13%` hit rate | `directional_no_go` |

Materiality uplift над signed-reaction/context baseline по calibrated AUC —
`+0,0434`, day-cluster bootstrap 95% CI `[+0,0155; +0,0711]`. Это эффект
absolute transforms первых пяти минут, а не текста/LLM. Publication-only
версия имеет raw ROC-AUC `0,6751`.

На 157 сохранённых строках config 6 directional ML показала hit rate `51,59%`,
формула — `50,96%`; 95% interval paired-разницы
`[-12,10; +12,42] п.п.` включает ноль. Это не превосходство и не сравнение с
текущим live config 7. Канонический directional artifact сохраняет именно
`update_5m_corrected_reaction_interactions`; generic fresh artifact — отдельно
версионированный `update_5m_common_direction_logistic` без этих метрик.

## Provenance и границы выводов

- Telegram public pages дают текущую видимую редакцию, а не доказанный первый
  historical snapshot. Для архива `received_at = published_at + 5m` — явное
  исследовательское допущение.
- v2 относится только к fresh Telegram admission. Старый канонический v2
  checkpoint включает историческую LLM-review/feature стадию, которую нельзя
  вывести из номера схемы или побайтово восстановить новым builder.
- YDB manifest `complete=true` означает завершённый bounded export, но страницы
  и таблицы читаются независимыми paginated `SELECT` без общей snapshot.
  Отдельные signal/evaluation exports также не образуют coherent snapshot.
- Семантическая часть канонического корпуса создана разными extractor-поколениями
  и не является human gold set. Исторические окна многократно использовались
  при разработке, поэтому это не untouched test.
- Несколько строк могут описывать один event/price path; split и оценка обязаны
  учитывать information groups. MOEX redistribution и права на raw Telegram
  проверяются отдельно перед передачей данных.
- Комиссии, проскальзывание, ликвидность и надёжные corporate-action
  корректировки не входят в эти метрики. Ни одна модель не является обещанием
  доходности или автоматической торговой рекомендацией.

## Runtime inference и безопасный rollout

Retained materiality artifact поставляется вместе с Python-пакетом как
`src/eventedge/models/news-materiality-v1.json`. Runtime проверяет схему и
закреплённый payload SHA-256 до первого предсказания и вычисляет logistic score
без `pickle`, `sklearn` или исполнения кода из артефакта.

Пятиминутный maintenance-контур выполняет следующую идемпотентную цепочку:

1. выбирает company-news с поддерживаемым тикером не раньше
   `published_at + 5m` и не старше семи дней;
2. получает минутные opens акции и IMOEX2, а также causal daily history;
3. строит тот же `update_5m_symmetric_materiality` feature contract, не читая
   свечи после `data_cutoff_at`;
4. сохраняет raw/calibrated probability, missing features, версии
   model/features и SHA артефакта в YDB-таблицу `materiality_predictions`;
5. повторяет временно отложенные market-data ошибки не чаще одного раза в
   десять минут.

Один запуск ограничен `NEWS_MATERIALITY_BATCH_LIMIT` (по умолчанию 8), а
сетевой fan-out — двумя кандидатами. `market_materiality` появляется в
`GET /v1/news` только для текущей версии модели. Поддерживаются три режима:

- `NEWS_MATERIALITY_MODE=disabled` — модель не загружается и inference не
  выполняется; это default;
- `shadow` — scores сохраняются и показываются, но canonical API order не
  меняется;
- `rank` — готовые scores могут переставлять только in-domain записи в уже
  занимаемых ими позициях одного UTC-дня. Свежие pending и out-of-domain
  новости не опускаются ниже из-за отсутствующего score.

Web UI сохраняет canonical order API, показывает вероятность заметной реакции
для admitted scores и даёт явную сортировку «По ML-значимости». Вероятность
означает `abs(abnormal 4h) >= 0,5 п.п.`, а не направление цены.

Проверка runtime-контура локально:

```bash
uv run pytest tests/test_materiality.py \
  tests/test_materiality_pipeline.py tests/test_storage.py tests/test_api.py
uv run python scripts/validate_openapi.py
cd web && npm test && npm run build
```

Deployment workflow требует явного выбора режима и по умолчанию передаёт
`disabled`. Перед `shadow` migration создаёт prediction table; переход к
`rank` делается только после оценки нового live shadow-окна. Само добавление
кода и локальные команды production не меняют.

## Следующий продуктовый шаг

В production пока остаётся rule-based `signal-engine-0.6.1`,
`rules-0.3.0`, `config_version=7`. Он исправляет полярность финансовых
изменений, сохраняет neutral при отрицании/условности/конфликте и не должен
переписываться этим исследовательским контуром.

Live extraction сначала применяет causal candidate gate, затем принимает
структурированный LLM-ответ только с цитатами и значениями, найденными во
входном тексте; иначе используется deterministic fallback. Эти semantic
regression guarantees покрывают известные ошибки, но сами по себе не доказывают
рост market hit rate.

Следующий шаг — явно включить materiality в `shadow`, не меняя config-7 output,
и измерить на новом live-окне coverage, calibration, ROC/PR-AUC, top-k
precision, стабильность по источникам и ошибки доступности данных. Только после
этого можно переключать `rank`. Directional output остаётся выключенным до
отдельного устойчивого преимущества над market controls и формулой.

Канонические числа, пути и SHA-256 зафиксированы в
[`ML_ARTIFACT_MANIFEST.json`](ML_ARTIFACT_MANIFEST.json). Перенос локального
checkpoint bundle выполняется только через авторизованный защищённый канал без
service-account keys, токенов и endpoint credentials.
