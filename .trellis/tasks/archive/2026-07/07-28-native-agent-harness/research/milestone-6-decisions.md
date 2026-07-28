# Milestone 6 Provider and MCP Decisions

- The root package adds only provider-neutral `ReasoningDelta` and optional
  structured-output schema fields. It does not import an OpenAI or MCP SDK.
- `by-framework-model-openai` depends only on `by-framework`. An injected
  streaming transport owns HTTP authentication, connection pooling, and SSE
  decoding. The adapter translates OpenAI-compatible chunks into core text,
  reasoning, completed tool calls, usage, finish reason, and provider metadata.
- Retries occur only when a retryable/rate-limit failure happens before any
  normalized output was emitted. Retrying a partially emitted stream would
  duplicate user-visible output and is deliberately rejected.
- Provider authentication, rate-limit, timeout/server, malformed-response, and
  incomplete tool-call failures use explicit normalized exceptions.
- `by-framework-tools-mcp` also depends only on `by-framework`. An injected MCP
  client/session owns concrete transport and SDK objects. `MCPToolset` owns
  initialization, discovery caching/refresh, calls, cancellation, and close.
- Discovered MCP definitions become `ToolSpec`-compatible dynamic tools and run
  through the core `ToolExecutor`. Structured MCP errors retain code and data.

## Validation Boundary

- Contract tests use fake transports and sessions; no network credentials or
  external services are required.
- The OpenAI-compatible adapter currently targets chat-completions-style decoded
  chunks. A concrete HTTP package may adapt Responses API events into the same
  injected chunk contract or extend the normalizer with recorded fixtures.
- Structured output requests are transmitted and normalized text is returned;
  schema enforcement remains the provider's responsibility until a core
  output-validator policy is added.
- MCP transport negotiation, stdio/SSE process management, protocol-version
  compatibility, and resource/prompt APIs remain responsibilities of a concrete
  MCP client package. This package covers tool discovery and execution only.
