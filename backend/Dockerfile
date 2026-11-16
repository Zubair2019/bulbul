# Bulbul backend: FastAPI + LangGraph, served by uvicorn on port 8000, with live voice
# (../voicecat, mounted at /api/voice). Build context is the repo root (see docker-compose.yml).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/backend/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app/backend

# Install dependencies first so code changes don't invalidate this layer.
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
COPY voicecat/pyproject.toml /app/voicecat/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-install-package voicecat

# voicecat itself (editable, so its config and models/ folder stay at /app/voicecat).
COPY voicecat/README.md /app/voicecat/README.md
COPY voicecat/config /app/voicecat/config
COPY voicecat/voicecat /app/voicecat/voicecat
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY backend/run.py ./
COPY backend/config ./config
COPY backend/app ./app
COPY backend/stubs ./stubs

# Non-root user, a writable state dir for named volumes (e.g. the Swiggy stub's /data), and the
# speech models folder (bind-mounted from ./voicecat/models).
RUN useradd --create-home --uid 1000 bulbul \
    && mkdir -p /data /app/voicecat/models && chown bulbul:bulbul /data /app/voicecat/models
USER bulbul

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"

CMD ["python", "run.py"]
