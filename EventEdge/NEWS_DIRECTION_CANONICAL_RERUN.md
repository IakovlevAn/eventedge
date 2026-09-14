# Канонический перезапуск пяти экспериментов направления

## Итог

Все пять этапов перезапущены на каноническом датасете из 1 238 строк с SHA-256
`ee47437edd61f2f444f32e1d85e7562fb0a07c47750651217ffc898c55ca632c`.
Направление измеряется как знак abnormal return от `publication + 5 минут` до
`publication + 240 минут`. Состав семи evaluation-folds полностью совпал с
`NEWS_MODEL_BENCHMARK_CONTRACT.json`; в них 480 событий. Контрольная формула
config 6 воспроизвела зафиксированные 157 строк и hit rate `50,96%`.

Ни один эксперимент не даёт подтверждённого улучшения направления. Лучший
наблюдавшийся результат среди расширенных рыночных признаков — ROC-AUC `0,5435`,
hit rate `54,17%` и balanced accuracy `54,82%` у
`reaction_microstructure_finbert`, но это post-hoc максимум среди нескольких
вариантов. Парные 95% интервалы улучшения относительно `reaction_core` включают
ноль: для ROC-AUC `[-0,0181; 0,0736]`, для hit rate
`[-3,74; 6,53]` процентного пункта. Это исследовательский сигнал, а не основание
для внедрения.

Эти числа относятся к общей ablation-рамке этапов 1–5 и дополняют, но не
перезаписывают frozen benchmark PR #148: там сохранены другой фиксированный
feature contract и calibration. Совпадают канонические строки, folds и target,
однако выбирать «лучшую из всех» по этим повторно использованным evaluation
окнам нельзя.

Положительный вывод остался только для значимости: `reaction_core` повышает
PR-AUC с `0,6419` до `0,6855`, а парный 95% интервал прироста
`[0,0161; 0,0721]` выше нуля. Поэтому продуктовая рекомендация не меняется:
ранжировать важные события портфеля и объяснять их, а направление не показывать
как надёжный прогноз.

## Результаты по этапам

| Этап | Что проверяли | Главный результат | Решение |
| --- | --- | --- | --- |
| 1 | Frozen RuBERT sentiment | direct ROC-AUC `0,5331`, hit rate `50,00%`; добавление в logistic снизило hit rate на config-6 subset с `50,32%` до `49,04%` | No-go |
| 2 | Рыночный контекст до `publication + 5m` | лучший direction ROC-AUC `0,5435`, hit rate `54,17%`, но оба paired CI включают ноль; materiality PR-AUC `0,6855` подтверждён | Materiality — да, direction — нет |
| 3 | Горизонты, abstention, neutral | максимум `56,67%` при coverage `25%`, cluster CI `[47,29%; 65,74%]`; прошедших gate конфигураций нет | No-go |
| 4 | Структурированные признаки и TF-IDF | `structured_full` ROC-AUC `0,5100`, hit rate `49,58%`; TF-IDF ROC-AUC `0,5018` | No-go |
| 5 | HistGradientBoosting, ExtraTrees и blend | лучший nonlinear ROC-AUC `0,5206`, hit rate `50,63%`; reaction-core остаётся лучше по hit rate (`52,71%`) | No-go |

## Контроли и воспроизведение

Канонический файл восстановлен полностью офлайн из исходного датасета
`ac639e…` и двух frozen MOEX-кэшей первого архива. Получен byte-exact SHA
`ee4743…`; изменились только benchmark-поля, включая 23 времени доступности
label. Тексты не менялись, поэтому frozen RuBERT inference повторно использован
только после проверки всех `id`, text SHA и revision модели.

Полный запуск:

```bash
MPLCONFIGDIR=.local-artifacts/matplotlib \
PYTHONPATH=src:. uv run --locked --group research python \
  experiments/canonical_rerun/run_experiment.py
```

Веса модели, датасеты, свечи, predictions и полный `results.json` остаются в
игнорируемом `.local-artifacts/`. В git фиксируются runner, notebook, этот отчёт
и сокращённый машинно-читаемый контракт. Production-код, model artifact и
rollout mode не меняются.
