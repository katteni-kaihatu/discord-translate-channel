FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_NO_DEV=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY main.py translate.py ./
RUN mkdir /data && chown 1000:1000 /data

LABEL org.opencontainers.image.source="https://github.com/katteni-kaihatu/discord-translate-channel"

USER 1000:1000
CMD ["/app/.venv/bin/python", "main.py"]
