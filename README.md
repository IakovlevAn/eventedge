# EventEdge

EventEdge — API-first платформа краткосрочных сигналов по российским акциям.

Текущий live-контур сочетает новостной сигнал с детерминированными рыночными
факторами. `GET /v1/assessments` возвращает оценки всего списка наблюдения, а
`GET /v1/evals` — фактическую реакцию цены и демо-счёт с издержками.

- [Понятное описание продукта](EventEdge/README.md)
- [Технический дизайн](EventEdge/TECHNICAL_DESIGN.md)
- [Направление UX/UI](EventEdge/DESIGN_SYSTEM.md)
- [OpenAPI-контракт](EventEdge/openapi.yaml)
- [Подключение Yandex Cloud](EventEdge/CLOUD_SETUP.md)

## Локальная проверка

```bash
uv sync --locked --dev
uv run ruff check .
uv run pytest
uv run python scripts/validate_openapi.py
```

## Локальный UI

```bash
cd web
npm install
npm run dev
```
