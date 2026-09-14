# Малые нелинейные модели направления — этап 5

## Короткий вывод

Этап 5 **не решил направление**. Primary HistGradientBoosting получил ROC-AUC **0,5155** и hit rate **50,21%**. Лучший ranking показал ExtraTrees — ROC-AUC **0,5369**, но cluster-bootstrap 95% CI **[0,4847; 0,5882]**; прирост против `reaction_core` равен +0,0317 с CI **[−0,0314; +0,0945]**. Лучший hit rate у фиксированного 50/50 blend — **53,33%**, CI **[48,95%; 57,71%]**. Ни одна модель не прошла production-gate.

53,33% blend не является полноценным преимуществом: в evaluation 53,33% событий имеют класс `down`, поэтому тривиальный always-down даёт такую же обычную accuracy. Blend не выродился в always-down и имеет balanced accuracy 52,65%, однако статистически подтверждённого преимущества всё равно нет.

## Зафиксированный протокол

Primary target — знак четырёхчасового abnormal return акции относительно IMOEX2. Primary challenger — `HistGradientBoostingClassifier`; ExtraTrees и их фиксированный 50/50 blend объявлены диагностическими моделями до запуска. Базовые OOF-прогнозы `reaction_core` и `structured_logistic` заблокированы из этапа 4 и не переоптимизировались.

Production-gate сохранён без изменений:

1. Hit rate не ниже 58%.
2. Нижняя граница cluster-bootstrap 95% CI hit rate выше 50%.
3. Hit rate выше 50% минимум в пяти из семи folds.
4. Нижняя граница cluster-bootstrap 95% CI прироста ROC-AUC против `reaction_core` выше нуля.

Оценка проведена на тех же 480 событиях марта–сентября 2026 года: семь walk-forward folds, обучение только на прошлом, два предыдущих месяца validation, embargo 72 часа и purge повторов по `event_group_id`. Hyperparameters выбирались по validation ROC-AUC, threshold — по validation balanced accuracy. После выбора модель не дообучалась на validation, чтобы не переносить порог на другую шкалу вероятностей.

## Модели и ограничение сложности

В модель не передавались текст, RuBERT/FinBERT embeddings или TF-IDF. Использованы `reaction_core` и только наиболее прямые признаки этапа 4:

- comparison/percent-change signal и их сила;
- special-event signal и общий structured signal;
- наличие консенсуса и сравнения с предыдущим периодом;
- event type/subtype и сектор;
- режим IMOEX2, relative momentum, первые пять минут abnormal reaction и часть торговой сессии;
- прежние rule/context признаки baseline.

HistGradientBoosting ограничен 7/15 листьями, 150 итерациями, minimum leaf 20/40 и L2 1/5. ExtraTrees содержит 128 деревьев глубиной 3/5/8 с minimum leaf 5/15. Grid фиксирован заранее и выбирается только на validation.

## Результаты на 480 событиях

| Модель | ROC-AUC | Hit rate | Balanced accuracy | 95% CI hit rate | Folds > 50% |
|---|---:|---:|---:|---:|---:|
| `reaction_core` | 0,5053 | 52,71% | 52,40% | [48,20%; 56,96%] | 5/7 |
| `structured_logistic` | 0,5118 | 51,46% | 50,08% | [47,18%; 55,83%] | 5/7 |
| `hist_gradient_boosting` | 0,5155 | 50,21% | 49,58% | [45,82%; 54,60%] | 2/7 |
| `extra_trees` | **0,5369** | 52,92% | 52,15% | [48,43%; 57,38%] | 2/7 |
| `nonlinear_blend` | 0,5313 | **53,33%** | **52,65%** | [48,95%; 57,71%] | 6/7 |
| Тривиальный always-down | — | **53,33%** | 50,00% | — | — |

| Challenger против `reaction_core` | Δ ROC-AUC | 95% CI | Δ hit rate | 95% CI |
|---|---:|---:|---:|---:|
| `structured_logistic` | +0,0065 | [−0,0362; +0,0491] | −1,25 п.п. | [−6,03; +3,34 п.п.] |
| `hist_gradient_boosting` | +0,0103 | [−0,0485; +0,0651] | −2,50 п.п. | [−7,74; +2,91 п.п.] |
| `extra_trees` | **+0,0317** | [−0,0314; +0,0945] | +0,21 п.п. | [−5,72; +5,83 п.п.] |
| `nonlinear_blend` | +0,0260 | [−0,0327; +0,0823] | +0,63 п.п. | [−4,32; +5,58 п.п.] |

ExtraTrees даёт наиболее интересный исследовательский намёк, но не production-результат: CI включает отсутствие эффекта, а только 2 из 7 folds строго выше 50% hit rate. Blend устойчивее по fold hit rate, но его общая accuracy совпадает с majority baseline и нижняя граница CI ниже 50%.

## Сравнение с config 6

На фиксированных 157 событиях config 6 получила 50,96%. HistGradientBoosting также дал 50,96%, ExtraTrees — 49,04%, blend — 53,50%. Для blend разница против формулы составляет +2,55 п.п., cluster-bootstrap 95% CI **[−9,49; +14,65 п.п.]**. Доказанного преимущества нет.

## Ресурсы

| Модель | Медианный размер выбранной модели | Максимальный размер | Медианное число transformed features |
|---|---:|---:|---:|
| HistGradientBoosting | 296 КБ | 297 КБ | 100 |
| ExtraTrees | 203 КБ | 348 КБ | 100 |
| Blend | 491 КБ | 644 КБ | 100 |

Все варианты работают на CPU и не требуют model server, GPU или сетевых вызовов. То есть провал связан не со стоимостью: даже почти бесплатная nonlinear-модель не извлекает из текущих данных достаточно устойчивого directional signal.

## Контроль корректности

- Модельный feature contract запрещает `return_4h`, `label`, `entry_at`, `outcome`, `observed_at`, `target_at` и сырой `model_text`.
- Imputer и one-hot encoder обучаются внутри train каждого fold; validation и test не участвуют в preprocessing fit.
- Все hyperparameters и thresholds выбираются только на validation.
- Stage-4 OOF baselines присоединяются по уникальному `id` с one-to-one проверкой.
- Все 480 evaluation-строк уникальны, представлены ровно в одном test fold и сравниваются попарно.
- Доверительные интервалы перевыбирают целые `event_group_id`, а не считают дубликаты независимыми.
- Размеры моделей рассчитаны по сериализованным выбранным fold-моделям, а не по теоретической оценке.

## Итог всех пяти этапов

1. Frozen RuBERT sentiment не улучшил направление.
2. Рыночная реакция первых пяти минут доказанно улучшила **значимость**, но не направление.
3. Abstention, neutral-класс и альтернативные горизонты не дали надёжного direction.
4. Тип события, sector/regime и структурированный surprise дали лишь слабый ranking-сигнал.
5. Нелинейные модели подняли наблюдаемый ROC-AUC максимум до 0,5369, но эффект не подтверждён и не превращается в полезный hit rate.

Техническое решение: **остановить оптимизацию direction на текущем датасете и убрать направление из продуктового обещания**. Сервис следует позиционировать как отбор важных событий по портфелю и их объяснение, с возможным статусом «подтверждено первичной реакцией рынка через пять минут».

Возвращаться к direction имеет смысл только после появления принципиально новых данных, а не другой модели: машиночитаемого analyst consensus до события, факта относительно консенсуса, sector benchmark вместо одного IMOEX2, order-book/flow features, гарантированных первых snapshot Telegram и существенно большего числа независимых событий по каждому event subtype. До этого более тяжёлый transformer, CatBoost/XGBoost или дальнейший tuning создадут в основном риск переобучения.

## Воспроизведение

```bash
PYTHONPATH=src:. uv run --locked --group research python \
  experiments/direction_stage5/run_experiment.py
```

Generated метрики и predictions сохраняются в
`.local-artifacts/news-direction-legacy/direction_stage5`. Исполняемый разбор
находится в `direction_stage5.ipynb`.
