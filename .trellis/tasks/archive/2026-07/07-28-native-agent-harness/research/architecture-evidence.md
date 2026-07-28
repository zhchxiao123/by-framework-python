# Architecture Evidence

## Repository Findings

- `GatewayWorker` is already the correct outer lifecycle and compatibility boundary.
- `AgentContext` supplies distributed framework primitives but not a model/tool reasoning loop.
- `AgentConfig` contains the vocabulary of an Agent definition but lacks strong executable contracts.
- Current history persistence is a conversation view, not durable orchestration state.
- LangGraph and ADK packages demonstrate the adapter pattern and must remain peer integrations.
- The root dependency set supports keeping provider SDKs in optional packages.

## External Framework Comparison

### LangGraph

Official documentation positions LangGraph as a low-level orchestration runtime centered on durable execution, streaming, persistence and human-in-the-loop. Its checkpoint model enables fault tolerance, replay and state inspection.

- https://docs.langchain.com/oss/python/langgraph/overview
- https://docs.langchain.com/oss/python/langgraph/persistence
- https://docs.langchain.com/oss/python/langgraph/graph-api
- https://docs.langchain.com/oss/python/langgraph/functional-api

### OpenAI Agents SDK

The SDK is Agent-first: Agent plus Runner owns model turns, tools, handoffs, sessions and tracing. It distinguishes manager-style Agent-as-tool orchestration from handoff-based control transfer.

- https://openai.github.io/openai-agents-python/agents/
- https://openai.github.io/openai-agents-python/handoffs/
- https://openai.github.io/openai-agents-python/running_agents/
- https://openai.github.io/openai-agents-python/tracing/

### AutoGen

AutoGen separates a developer-friendly AgentChat layer from an event-driven Core runtime and provides Team patterns plus distributed runtime options.

- https://microsoft.github.io/autogen/stable/index.html
- https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/index.html

## Design Consequence

The native framework should combine an Agent-first authoring surface with a graph-capable durable runtime. Its differentiator is not a novel ReAct loop; it is the unification of Agent/Team authoring with Redis-native, cross-Worker durable execution and compatibility with existing external Agent workers.
