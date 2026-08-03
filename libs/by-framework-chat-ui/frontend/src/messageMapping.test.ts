import { describe, expect, it } from "vitest";
import { historyMessageToThreadMessage } from "./messageMapping";
import type { HistoryMessage } from "./api";

describe("historyMessageToThreadMessage", () => {
  it("maps a user message", () => {
    const record: HistoryMessage = { role: "user", content: "hi", is_ask_user: false };

    const message = historyMessageToThreadMessage(record, 0);

    expect(message.role).toBe("user");
    expect(message.content).toEqual([{ type: "text", text: "hi" }]);
  });

  it("maps a normal assistant message without the ask-user marker", () => {
    const record: HistoryMessage = {
      role: "assistant",
      content: "hello there",
      is_ask_user: false,
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
    };

    const message = historyMessageToThreadMessage(record, 1);

    expect((message as { metadata: { custom: Record<string, unknown> } }).metadata.custom).toEqual(
      { askUser: true },
    );
  });

  it("assigns each message a unique, stable id derived from its index", () => {
    const a = historyMessageToThreadMessage(
      { role: "user", content: "a", is_ask_user: false },
      0,
    );
    const b = historyMessageToThreadMessage(
      { role: "user", content: "b", is_ask_user: false },
      1,
    );

    expect(a.id).not.toBe(b.id);
  });
});
