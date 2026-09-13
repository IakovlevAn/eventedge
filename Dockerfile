FROM node:22-alpine AS web-builder

WORKDIR /web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web ./
RUN npm run build

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

RUN groupadd --system eventedge && useradd --system --gid eventedge eventedge

COPY pyproject.toml ./
COPY PACKAGE.md ./
COPY README.md ./
COPY configs ./configs
COPY src ./src
COPY --from=web-builder /src/eventedge/static ./src/eventedge/static

RUN pip install --no-cache-dir .

USER eventedge
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn eventedge.main:app --host 0.0.0.0 --port ${PORT}"]
