FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# deps first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY deploy ./deploy
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

# the sqlite store writes here; mount a volume on it to keep the database
RUN mkdir -p /app/data

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8791

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh
ENTRYPOINT ["/docker-entrypoint.sh"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8791"]
