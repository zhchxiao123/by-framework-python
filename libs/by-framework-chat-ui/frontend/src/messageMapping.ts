import type { ThreadMessage } from "@assistant-ui/react";
import type { HistoryMessage } from "./api";
import { parseToolArgs } from "./chatAdapter";

/**
 * Convert a persisted history message (from `GET /api/conversations/{id}`)
 * into an assistant-ui `ThreadMessage`, preserving the ask-user marker
 * (`is_ask_user`) as `metadata.custom.askUser` — the same marker
 * `chatAdapter.ts` attaches to a live `ask_user` event — so a reopened
 * Conversation renders past clarifying questions with the same distinct
 * bubble style as when they first arrived.
 */
export function historyMessageToThreadMessage(
  record: HistoryMessage,
  index: number,
): ThreadMessage {
  const id = `history-${index}`;
  const createdAt = new Date(record.created_at);
  const toolCallParts = (record.tool_calls ?? []).map((tc) => ({
    type: "tool-call" as const,
    toolCallId: tc.call_id,
    toolName: tc.name,
    argsText: tc.arguments,
    args: parseToolArgs(tc.arguments),
    result: tc.result ?? undefined,
  }));
  const content = [...toolCallParts, { type: "text" as const, text: record.content }];

  if (record.role === "user") {
    return {
      id,
      createdAt,
      role: "user",
      content,
      attachments: [],
      metadata: { custom: {} },
    } as unknown as ThreadMessage;
  }

  return {
    id,
    createdAt,
    role: "assistant",
    content,
    status: { type: "complete", reason: "stop" },
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom: record.is_ask_user ? { askUser: true } : {},
    },
  } as unknown as ThreadMessage;
}
