# Offline ReAct weather agent

This example shows the native model → tool → model loop without Redis, network
access, API keys, or environment variables. `ScriptedModel` makes both model
turns deterministic: the first requests `get_weather`; `Runner` executes it and
adds a `ToolMessage`; the second reads that tool result and produces the answer.

Run it from the repository root:

```bash
uv run python examples/native_agent/react_weather_agent.py
```

The output identifies the model's tool call, the value returned to the model,
streamed text, lifecycle events, and the final result.

## Use an OpenAI-compatible provider

The replacement point is the `model` value in `build_agent()`. Replace its
`ScriptedModel(...)` with the workspace provider and pass that same `model` to
`Agent`:

```python
from by_framework_model_openai import OpenAICompatibleModel

model = OpenAICompatibleModel("your-model-name", transport)
```

`transport` is an application-supplied object whose async `stream(payload,
*, timeout)` method yields decoded OpenAI-compatible chunks. The provider uses
structural typing for that transport boundary; no transport base class needs to
be imported. Construct or inject the HTTP/SDK transport in application code.
This offline example intentionally does not copy a provider transport
implementation or make a live request.

## Run against a real OpenAI model

The complete Chat Completions SSE transport is in
`openai_weather_agent.py`. It reads credentials from the environment:

```bash
export OPENAI_API_KEY="your-api-key"
export OPENAI_MODEL="gpt-5.6-terra"  # optional
export OPENAI_BASE_URL="https://api.openai.com/v1"  # optional

uv run python examples/native_agent/openai_weather_agent.py
```

`OPENAI_BASE_URL` can point at another OpenAI-compatible endpoint. The endpoint
must implement streaming `POST /chat/completions` and OpenAI-compatible
function calling. Tests use `httpx.MockTransport` and never make a live request.

## Runtime context

Standalone runs create local session identity automatically. Applications can
pass an explicit context when tools need session capabilities:

```python
from by_framework.agent import RunContext, RunIdentity, Runner

context = RunContext(
    RunIdentity("session-1", "run-1", "weather-agent"),
    private_files=my_private_file_manager,
    shared_files=my_shared_file_manager,
)
result = await Runner().run(agent, "Weather?", context=context)
```

A tool opts into those capabilities with an injected parameter:

```python
from by_framework.agent import ToolExecutionContext

async def read_note(path: str, context: ToolExecutionContext) -> dict:
    files = context.run.require("private_files")
    return await files.read_file(path)
```

The injected parameter is omitted from the model-visible tool schema.
`NativeAgentWorker` builds the same context from its existing `AgentContext`,
including session, user, trace, files, history, and configuration access.
