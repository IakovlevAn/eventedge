FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

RUN groupadd --system eventedge && useradd --system --gid eventedge eventedge

COPY pyproject.toml ./
COPY README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

USER eventedge
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn eventedge.main:app --host 0.0.0.0 --port ${PORT}"]
