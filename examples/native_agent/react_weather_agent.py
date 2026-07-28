"""Offline ReAct weather-agent example using the native Agent API."""

import asyncio

from by_framework.agent import (
    Agent,
    FunctionTool,
    ModelResponse,
    Runner,
    ScriptedModel,
    ToolCall,
    Usage,
)

FINAL_ANSWER = "The weather in San Francisco is 18°C and sunny."


def get_weather(city: str) -> dict[str, str | int]:
    """Return deterministic weather for a city (no network request)."""
    return {"city": city, "condition": "sunny", "temperature_c": 18}


def build_agent() -> tuple[Agent, ScriptedModel]:
    """Build the deterministic teaching agent and expose its model for inspection."""
    model = ScriptedModel(
        [
            # ReAct "Act": the first model turn asks Runner to call a tool.
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "weather-1",
                        "get_weather",
                        {"city": "San Francisco"},
                    ),
                ),
                usage=Usage(input_tokens=12, output_tokens=5),
                finish_reason="tool_calls",
            ),
            # ReAct "Reason": this turn receives the ToolMessage and answers.
            ModelResponse(
                text=FINAL_ANSWER,
                usage=Usage(input_tokens=24, output_tokens=12),
            ),
        ],
        text_chunk_size=24,
    )

    # Replacement point for a real provider: pass an OpenAICompatibleModel
    # instance here instead of ScriptedModel. See this directory's README.
    agent = Agent(
        "weather-assistant",
        "Use get_weather when asked about weather, then answer from its result.",
        model,
        [FunctionTool(get_weather)],
    )
    return agent, model


async def run_example():
    """Run the example and return its events, result, and recorded model."""
    agent, model = build_agent()
    stream = Runner().run_streamed(
        agent,
        "What is the weather in San Francisco?",
        run_id="react-weather-example",
    )
    events = []
    async for event in stream:
        events.append(event)
        if event.kind == "tool_call":
            print(f"MODEL -> TOOL: {event.data['name']}" f"({event.data['arguments']})")
        elif event.kind == "tool_result":
            print(f"TOOL -> MODEL: {event.data['output']}")
        elif event.kind == "text_delta":
            print(f"MODEL TEXT: {event.data['text']}")
        elif event.kind in {"run_started", "run_completed"}:
            print(f"EVENT: {event.kind}")

    result = await stream.result()
    print(f"FINAL RESULT: {result.output}")
    return events, result, model


if __name__ == "__main__":
    asyncio.run(run_example())
