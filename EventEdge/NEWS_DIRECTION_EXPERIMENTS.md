# Пять дополнительных экспериментов направления

## Статус

Это воспроизводимый архив historical development-экспериментов, выполненных до
фиксации canonical pipeline в PR #148. Он не меняет production, не добавляет
directional-модель и не переопределяет метрики из `ML_PIPELINE.md`.

Исходный датасет архива имеет SHA-256 `ac639e944a100c83b91275bbf627d3e9af9c654e5a0ce726dc484c0e2c0b81b6`.
Canonical dataset имеет SHA-256 `ee47437edd61f2f444f32e1d85e7562fb0a07c47750651217ffc898c55ca632c`.
Также различается directional target: архив измеряет горизонт от `entry_at`, а
canonical benchmark — оставшееся движение до `publication + 240 минут`.
Поэтому результаты ниже являются отдельным свидетельством и требуют rerun для
прямого объединения с canonical benchmark.

## Результаты

| Этап | Проверка | Результат | Решение |
| --- | --- | --- | --- |
| 1 | Frozen RuBERT sentiment | Δ PR-AUC materiality `+0,0019`, CI включает ноль; direction не улучшен | Не внедрять |
| 2 | Рыночные признаки до `publication + 5m` | `reaction_core` поднял PR-AUC materiality с `0,6419` до `0,6855` | Положительный development-сигнал; canonical runtime уже использует развитие этой идеи |
| 3 | Горизонты 30/60/120/240m, abstention и neutral | Лучший практически релевантный hit rate `52,68%`, CI включает `50%` | Не внедрять |
| 4 | Структурированные признаки и TF-IDF | `structured_full` ROC-AUC `0,5118`, hit rate `51,46%` | Не внедрять |
| 5 | HistGradientBoosting, ExtraTrees и blend | Лучший hit rate `53,33%`, равен majority-class baseline | Не внедрять |

Совокупный вывод: пятиминутная абсолютная реакция полезна для materiality, но
ни sentiment, ни selective prediction, ни структурированные признаки, ни малые
нелинейные модели не дают подтверждённого преимущества для direction.

## Воспроизводимость и границы

- evaluation: 480 событий марта–сентября 2026 года;
- семь последовательных monthly folds;
- два предыдущих месяца validation;
- train содержит только более ранние доступные labels;
- embargo 72 часа;
- повторы не разделяются между partitions;
- выбор порогов выполняется на validation;
- доверительные интервалы кластеризованы по `event_group_id`;
- CI, post-hoc сравнения и отрицательные результаты сохраняются, а не
  отбрасываются.

Исполняемый архив находится в `experiments/`, а точные статусы и headline
метрики — в `NEWS_DIRECTION_EXPERIMENTS_CONTRACT.json`. Raw dataset, свечи,
веса модели и generated predictions не хранятся в git.

## Следующий шаг

Перед изменением canonical benchmark все пять этапов необходимо перезапустить
на dataset SHA `ee4743...`, используя его folds и target definitions. До этого
архив не должен менять `ML_ARTIFACT_MANIFEST.json`, production artifact или
rollout mode.
