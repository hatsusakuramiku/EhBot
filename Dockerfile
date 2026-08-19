FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install --no-install-recommends --yes 7zip ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

RUN groupadd --system --gid 10001 ehbot \
    && useradd --system --uid 10001 --gid ehbot --home-dir /app ehbot \
    && mkdir -p /app/data /library /work \
    && chown -R ehbot:ehbot /app /library /work

USER ehbot
EXPOSE 8080

CMD ["python", "-m", "app.server"]
