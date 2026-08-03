/**
 * Interpret the chat-ui WebSocket wire protocol into UI-facing turn state.
 *
 * Mirrors the backend's `by_framework_chat_ui.protocol` module: one place
 * that understands the wire shape, so the rest of the frontend works with a
 * small explicit turn-state shape instead.
 */

export type ServerEvent =
  | { type: "chunk"; content: string }
  | { type: "final"; content: string }
  | { type: "ask_user"; prompt: string }
  | { type: "turn_complete" }
  | { type: "locked" }
  | { type: "unlocked" }
  | { type: "error"; message: string }
  | { type: "tool_call"; call_id: string; name: string; arguments: string }
  | { type: "tool_result"; call_id: string; content: string; tool_name: string }
  | { type: "other" };

export interface ToolCallState {
  toolCallId: string;
  toolName: string;
  argsText: string;
  result?: string;
}

export interface TurnState {
  accumulated: string;
  isAskUser: boolean;
  done: boolean;
  error: string | null;
  toolCalls: ToolCallState[];
}

export function initialTurnState(): TurnState {
  return { accumulated: "", isAskUser: false, done: false, error: null, toolCalls: [] };
}

export function isLockEvent(
  event: ServerEvent,
): event is { type: "locked" } | { type: "unlocked" } {
  return event.type === "locked" || event.type === "unlocked";
}

export function applyServerEvent(state: TurnState, event: ServerEvent): TurnState {
  switch (event.type) {
    case "chunk":
      return { ...state, accumulated: state.accumulated + event.content };
    case "final":
      return { ...state, accumulated: event.content };
    case "ask_user":
      return { ...state, accumulated: event.prompt, isAskUser: true, done: true };
    case "turn_complete":
      return { ...state, done: true };
    case "error":
      return { ...state, error: event.message, done: true };
    case "tool_call":
      return {
        ...state,
        toolCalls: [
          ...state.toolCalls,
          {
            toolCallId: event.call_id,
            toolName: event.name,
            argsText: event.arguments,
          },
        ],
      };
    case "tool_result": {
      const hasMatch = state.toolCalls.some((tc) => tc.toolCallId === event.call_id);
      if (!hasMatch) {
        // No matching prior ToolCall this turn — not expected by the wire
        // protocol, but not enforced either; render a placeholder rather
        // than silently dropping the result.
        return {
          ...state,
          toolCalls: [
            ...state.toolCalls,
            {
              toolCallId: event.call_id,
              toolName: event.tool_name || "tool",
              argsText: "",
              result: event.content,
            },
          ],
        };
      }
      return {
        ...state,
        toolCalls: state.toolCalls.map((tc) =>
          tc.toolCallId === event.call_id
            ? { ...tc, result: event.content, toolName: tc.toolName || event.tool_name }
            : tc,
        ),
      };
    }
    case "locked":
    case "unlocked":
    case "other":
      return state;
    default:
      return state;
  }
}
