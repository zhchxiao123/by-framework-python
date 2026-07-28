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
