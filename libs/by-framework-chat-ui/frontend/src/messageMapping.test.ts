import { describe, expect, it } from "vitest";
import { historyMessageToThreadMessage } from "./messageMapping";
import type { HistoryMessage } from "./api";

describe("historyMessageToThreadMessage", () => {
  it("maps a user message", () => {
    const record: HistoryMessage = {
      role: "user",
      content: "hi",
      is_ask_user: false,
      created_at: "2026-08-03T09:05:00+00:00",
      tool_calls: [],
    };

    const message = historyMessageToThreadMessage(record, 0);

    expect(message.role).toBe("user");
    expect(message.content).toEqual([{ type: "text", text: "hi" }]);
  });

  it("maps a normal assistant message without the ask-user marker", () => {
    const record: HistoryMessage = {
      role: "assistant",
      content: "hello there",
      is_ask_user: false,
      created_at: "2026-08-03T09:05:00+00:00",
      tool_calls: [],
    };

    const message = historyMessageToThreadMessage(record, 1);

    expect(message.role).toBe("assistant");
    expect((message as { metadata: { custom: Record<string, unknown> } }).metadata.custom).toEqual(
      {},
    );
  });

  it("marks an assistant message persisted with is_ask_user as an ask-user message", () => {
    const record: HistoryMessage = {
      role: "assistant",
      content: "What is your name?",
      is_ask_user: true,
      created_at: "2026-08-03T09:05:00+00:00",
      tool_calls: [],
    };

    const message = historyMessageToThreadMessage(record, 1);

    expect((message as { metadata: { custom: Record<string, unknown> } }).metadata.custom).toEqual(
      { askUser: true },
    );
  });

  it("assigns each message a unique, stable id derived from its index", () => {
    const a = historyMessageToThreadMessage(
      {
        role: "user",
        content: "a",
        is_ask_user: false,
        created_at: "2026-08-03T09:05:00+00:00",
        tool_calls: [],
      },
      0,
    );
    const b = historyMessageToThreadMessage(
      {
        role: "user",
        content: "b",
        is_ask_user: false,
        created_at: "2026-08-03T09:05:00+00:00",
        tool_calls: [],
      },
      1,
    );

    expect(a.id).not.toBe(b.id);
  });

  it("parses the persisted created_at into the message's createdAt", () => {
    const record: HistoryMessage = {
      role: "user",
      content: "hi",
      is_ask_user: false,
      created_at: "2026-08-03T09:05:00+00:00",
      tool_calls: [],
    };

    const message = historyMessageToThreadMessage(record, 0);

    expect(message.createdAt).toEqual(new Date("2026-08-03T09:05:00+00:00"));
  });

  it("maps persisted tool_calls into tool-call content parts before the text part", () => {
    const record: HistoryMessage = {
      role: "assistant",
      content: "the answer is 2",
      is_ask_user: false,
      created_at: "2026-08-03T09:05:00+00:00",
      tool_calls: [
        {
          call_id: "call_1",
          name: "calculate",
          arguments: '{"expression": "1+1"}',
          result: "2",
        },
      ],
    };

    const message = historyMessageToThreadMessage(record, 0);

    expect(message.content).toEqual([
      {
        type: "tool-call",
        toolCallId: "call_1",
        toolName: "calculate",
        argsText: '{"expression": "1+1"}',
        args: { expression: "1+1" },
        result: "2",
      },
      { type: "text", text: "the answer is 2" },
    ]);
  });
});
