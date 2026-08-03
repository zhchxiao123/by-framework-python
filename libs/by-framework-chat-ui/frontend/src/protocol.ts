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
  | { type: "other" };

export interface TurnState {
  accumulated: string;
  isAskUser: boolean;
  done: boolean;
  error: string | null;
}

export function initialTurnState(): TurnState {
  return { accumulated: "", isAskUser: false, done: false, error: null };
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
    case "locked":
    case "unlocked":
    case "other":
      return state;
    default:
      return state;
  }
}
