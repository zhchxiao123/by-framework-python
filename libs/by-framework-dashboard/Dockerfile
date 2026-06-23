# syntax=docker/dockerfile:1

FROM node:22-bookworm-slim AS frontend

WORKDIR /app/libs/by-framework-dashboard/frontend
COPY libs/by-framework-dashboard/frontend/package*.json ./
RUN npm ci --no-audit --progress=false
COPY libs/by-framework-dashboard/frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY libs/by-framework-dashboard ./libs/by-framework-dashboard
COPY libs/by-framework-trace-query ./libs/by-framework-trace-query
COPY --from=frontend /app/libs/by-framework-dashboard/src/by_framework_dashboard/static/ ./libs/by-framework-dashboard/src/by_framework_dashboard/static/

RUN uv venv /opt/venv \
    && uv build --wheel --project . --out-dir /tmp/wheels \
    && uv build --wheel --project libs/by-framework-trace-query --out-dir /tmp/wheels \
    && uv build --wheel --project libs/by-framework-dashboard --out-dir /tmp/wheels \
    && uv pip install --python /opt/venv/bin/python \
        /tmp/wheels/by_framework-*.whl \
        /tmp/wheels/by_framework_trace_query-*.whl \
        /tmp/wheels/by_framework_dashboard-*.whl \
    && rm -rf /tmp/wheels

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import json, urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3); json.load(response)"

CMD ["by-framework-dashboard", "--host", "0.0.0.0", "--port", "8765"]
