# by-framework-dashboard

Dashboard UI and HTTP server for inspecting by-framework workers, queues,
executions, sessions, traces, and Prometheus metrics.

## Quick Start

From the repository root:

```bash
uv run --package by-framework-dashboard by-framework-dashboard \
  --host 127.0.0.1 \
  --port 8765
```

Then open:

```text
http://127.0.0.1:8765
```

## Container Image

Dashboard release tags also publish a GHCR image:

```text
ghcr.io/<owner>/by-framework-dashboard:<version>
```

For example, pushing `by-framework-dashboard-v0.1.0` builds and publishes:

```text
ghcr.io/<owner>/by-framework-dashboard:0.1.0
ghcr.io/<owner>/by-framework-dashboard:latest
```

To run the image:

```bash
docker run --rm -p 8765:8765 \
  -e REDIS_HOST=host.docker.internal \
  ghcr.io/<owner>/by-framework-dashboard:0.1.0
```

For demo data without a live Redis cluster:

```text
http://127.0.0.1:8765?demo=1
```

## Redis Configuration

The dashboard reads live data from the same Redis instance used by workers and
clients. You can configure Redis with CLI flags:

```bash
uv run --package by-framework-dashboard by-framework-dashboard \
  --host 127.0.0.1 \
  --port 8765 \
  --redis-host localhost \
  --redis-port 6379 \
  --redis-db 0
```

Or with environment variables:

```bash
export REDIS_HOST=localhost
export REDIS_PORT=6379
export REDIS_DB=0
export REDIS_USERNAME=
export REDIS_PASSWORD=

uv run --package by-framework-dashboard by-framework-dashboard
```

Supported Redis env vars:

- `REDIS_HOST`
- `REDIS_PORT`
- `REDIS_DB`
- `REDIS_USERNAME`
- `REDIS_PASSWORD`
- `REDIS_MAX_CONNECTIONS`

## Authentication

When binding to a non-localhost interface, configure a bearer token:

```bash
export BY_FRAMEWORK_DASHBOARD_TOKEN="change-me"
uv run --package by-framework-dashboard by-framework-dashboard \
  --host 0.0.0.0 \
  --port 8765
```

API and metrics requests must then include:

```text
Authorization: Bearer change-me
```

Static dashboard assets remain public; `/api/*` and `/metrics` are protected.

## Useful Endpoints

- `/` - dashboard UI
- `/?demo=1` - dashboard UI with demo data
- `/api/health` - dashboard process health
- `/api/workers` - worker and agent health snapshot
- `/api/queues` - Redis stream queue snapshot
- `/api/executions` - recent execution snapshot
- `/api/session?session_id=<session_id>` - session execution and event details
- `/api/traces?session_id=<session_id>` - trace summaries through Trace Read SDK
- `/api/traces?worker_id=<worker_id>` - trace summaries by worker
- `/api/traces?agent_type=<agent_type>` - trace summaries by agent type
- `/api/trace/<trace_id>` - single trace detail through Trace Read SDK
- `/api/trace/<trace_id>?session_id=<session_id>` - single trace with session hint
- `/api/trace/<trace_id>/timeline` - trace timeline payload
- `/metrics` - Prometheus metrics

`/api/traces` in live mode requires at least one of `session_id`, `worker_id`,
or `agent_type`.

## Trace Data

Trace APIs use `by-framework-trace-query` under the hood. In v1 the read source
is Redis:

- `by_framework:trace:{trace_id}`
- `by_framework:trace:spans:{trace_id}`
- session registry and session data stream fallback
- trace indexes by session, worker, and agent

If `BYAI_TRACE_FALLBACK_URL` is set, `/api/trace/<trace_id>` can fall back to an
external HTTP trace source when Redis has no spans for the requested trace.

## Frontend Development

The packaged dashboard serves built static files from:

```text
libs/by-framework-dashboard/src/by_framework_dashboard/static
```

The editable React/Vite frontend lives in:

```text
libs/by-framework-dashboard/frontend
```

To work on the frontend:

```bash
cd libs/by-framework-dashboard/frontend
npm install
npm run dev
```

After frontend changes, build and copy the generated assets into the package
static directory according to the repository release workflow.

## Troubleshooting

If the page loads but live data is empty:

- Confirm workers and clients use the same Redis host, port, and DB.
- Open `http://127.0.0.1:8765/api/health`.
- Try demo mode with `http://127.0.0.1:8765?demo=1`.
- Query a known trace directly with `/api/trace/<trace_id>`.
- Query trace lists with a filter, for example `/api/traces?session_id=<id>`.

If API requests return `unauthorized`, include the bearer token configured via
`--auth-token` or `BY_FRAMEWORK_DASHBOARD_TOKEN`.
