FROM node:22-bookworm-slim AS web-build
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.12.9 AS uv-binary

FROM python:3.12-slim-bookworm
WORKDIR /app
ENV CONTROL_DATA_DIR=/data \
    UV_PROJECT_ENVIRONMENT=/opt/control-venv \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/control-venv/bin:$PATH"
COPY --from=uv-binary /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project \
    && groupadd --gid 10001 control \
    && useradd --uid 10001 --gid 10001 --no-create-home control \
    && install -d -o control -g control /data
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY --from=web-build /build/dist ./web/dist
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).close()"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
