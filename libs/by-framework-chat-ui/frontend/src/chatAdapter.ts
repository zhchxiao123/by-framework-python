import type {
  ChatModelAdapter,
  ChatModelRunResult,
  ThreadMessage,
} from "@assistant-ui/react";
import { applyServerEvent, initialTurnState, type TurnState } from "./protocol";
import type { SessionSocket } from "./chatSocket";

function extractLatestUserText(messages: readonly ThreadMessage[]): string {
  const last = messages[messages.length - 1];
  if (!last) return "";
  return last.content
    .filter((part): part is { type: "text"; text: string } => part.type === "text")
    .map((part) => part.text)
    .join("");
}

function toRunResult(state: TurnState): ChatModelRunResult {
  return {
    content: [{ type: "text", text: state.accumulated }],
    metadata: state.isAskUser ? { custom: { askUser: true } } : undefined,
  };
}

/**
 * A `ChatModelAdapter` (assistant-ui's `LocalRuntime` extension point) that
 * speaks the chat-ui WebSocket protocol unchanged. `ASK_AGENT` vs `RESUME`
 * selection stays entirely server-side (see `conversations.py`'s
 * `next_action_type()`) — this adapter only ever sends `{content}`.
 *
 * Takes an already-created `SessionSocket` rather than creating its own, so
 * the same connection can also be used for lock-state broadcasts
 * (`useLockState`) — one Conversation, one WebSocket connection.
 */
export function createWebSocketChatAdapter(socket: SessionSocket): ChatModelAdapter {
  return {
    async *run({ messages, abortSignal }) {
      const content = extractLatestUserText(messages);

      await socket.waitForOpen();
      socket.send(content);

      let state = initialTurnState();
      for await (const event of socket.nextTurnEvents(abortSignal)) {
        state = applyServerEvent(state, event);
        if (state.error !== null) {
          throw new Error(state.error);
        }
        yield toRunResult(state);
        if (state.done) return;
      }
    },
  };
}
