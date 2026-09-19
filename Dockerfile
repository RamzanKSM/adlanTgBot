FROM node:20-bookworm-slim AS codex-cli
ARG CODEX_CLI_VERSION=0.155.1
RUN npm install -g @openai/codex@${CODEX_CLI_VERSION}

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --gid 10001 app && adduser --uid 10001 --ingroup app --disabled-password --gecos '' app

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --upgrade pip && pip install .

RUN mkdir -p /app/data && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["python", "-m", "app.main"]

FROM runtime AS ai-worker
COPY --from=codex-cli /usr/local /usr/local
CMD ["python", "-m", "app.ai_worker_main"]
