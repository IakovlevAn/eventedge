# EventEdge handoff — 10 августа 2026

## Текущее состояние production

- Production revision: `7a92dc05d19f82921b002e713d591943a45088da`.
- Последний продуктовый PR: [#83 — Evaluate market signals against MOEX indexes](https://github.com/IakovlevAn/eventedge/pull/83), merged и задеплоен.
- Фильтры Evals и новостей исправлены ранее в PR #81: вся строка опции кликабельна, выбранная эпоха меняется сразу.
- Ретро-пересчёт завершён: `remaining=0`.
- Live-сигналы на финальном срезе: 29 — 18 `down`, 11 `up`, 0 `neutral`; все на `signal-engine-0.6.1`.
- Evals `signal-engine-0.6.1`: 29 сигналов, 20 уже имеют короткий outcome, 2 ждут данных, 7 недоступны; coverage 69%.
- Raw time-series export содержит 529 наблюдений и явный `evaluation_benchmark`.
- Все четыре trigger активны: fast каждую минуту, discovery раз в 5 минут, maintenance раз в 5 минут, slow раз в 15 минут.

## Что изменено

- Рыночные и отраслевые сигналы больше не отбрасываются Evals.
- `RUEQ` оценивается по `IMOEX`; отраслевые коды — по соответствующим индексам MOEX ISS.
- Использованный индекс сохраняется и в outcomes, и в raw time-series export.
- Исторические эпохи не перезаписываются и остаются доступными через UI/API.
- Полный локальный gate: Ruff, 161 pytest, production web build.

## Известные проблемы — продолжать отсюда

1. **Worker contention.** Ручной maintenance завершился успешно, но перед этим получил 429/500 пять раз: минутный сбор, maintenance и остальные collectors конкурируют в одном worker с лимитом одного запроса. Следующий инфраструктурный инкремент — отдельный eval-worker либо гарантированный distributed lease/разнесённое расписание.
2. **Метаданные Evals.** `/v1/evals/export?dataset=timeseries` возвращает 529 raw-строк, но `model_epochs[].observations` в `/v1/evals` может временно показывать `0`. Проверить и устранить рассинхрон cache/meta; сами raw-данные в YDB не потеряны.
3. **Качество модели пока слабое.** Текущий hit rate на коротком окне — 30% при coverage 69%. Это диагностический результат, не доказательство alpha.
4. **Company coverage.** Текущая новая эпоха состоит из рыночных и отраслевых кодов. Нужен отдельный аудит прямых company-кандидатов и причин отказа, не ослабляя фильтр до нейтрального шума.

## Быстрая проверка

```bash
curl -sS 'https://d5d8smlpd6q241aquti1.kocrdvxt.apigw.yandexcloud.net/health/live'
curl -sS 'https://d5d8smlpd6q241aquti1.kocrdvxt.apigw.yandexcloud.net/v1/signals?limit=100'
curl -sS 'https://d5d8smlpd6q241aquti1.kocrdvxt.apigw.yandexcloud.net/v1/evals?model_version=signal-engine-0.6.1'
curl -sS 'https://d5d8smlpd6q241aquti1.kocrdvxt.apigw.yandexcloud.net/v1/evals/export?format=json&dataset=timeseries&model_version=signal-engine-0.6.1'
```

Не запускать повторный ретро-backfill без новой версии модели или явной причины: текущая очередь уже пуста.
