import { describe, expect, it } from "vitest";
import type { ChatModelRunOptions, ThreadMessage } from "@assistant-ui/react";
import { createWebSocketChatAdapter } from "./chatAdapter";
import { createSessionSocket } from "./chatSocket";
import { FakeSocket } from "./test-utils/fakeSocket";

function userMessage(text: string): ThreadMessage {
  return {
    id: "m1",
    role: "user",
    content: [{ type: "text", text }],
    attachments: [],
    createdAt: new Date(),
    metadata: { custom: {} },
  } as unknown as ThreadMessage;
}

function runOptions(text: string, signal = new AbortController().signal): ChatModelRunOptions {
  return {
    messages: [userMessage(text)],
    abortSignal: signal,
  } as unknown as ChatModelRunOptions;
}

async function collectYields<T>(gen: AsyncGenerator<T>, count: number): Promise<T[]> {
  const results: T[] = [];
  for (let i = 0; i < count; i++) {
    const next = await gen.next();
    if (next.done) break;
    results.push(next.value);
  }
  return results;
}

describe("createWebSocketChatAdapter", () => {
  it("sends the latest user message and yields cumulative chunk content", async () => {
    const fake = new FakeSocket();
    const socket = createSessionSocket("s1", () => fake as unknown as WebSocket);
    const adapter = createWebSocketChatAdapter(socket);

    const runPromise = collectYields(
      adapter.run(runOptions("hi")) as AsyncGenerator<any>,
      2,
    );
    fake.open();
    await Promise.resolve();
    fake.receive({ type: "chunk", content: "hel" });
    fake.receive({ type: "chunk", content: "lo" });

    const results = await runPromise;

    expect(fake.sent).toEqual([JSON.stringify({ content: "hi" })]);
    expect(results).toEqual([
      { content: [{ type: "text", text: "hel" }], metadata: undefined },
      { content: [{ type: "text", text: "hello" }], metadata: undefined },
    ]);
  });

  it("replaces content with the final text and stops after turn_complete", async () => {
    const fake = new FakeSocket();
    fake.open();
    const socket = createSessionSocket("s1", () => fake as unknown as WebSocket);
    const adapter = createWebSocketChatAdapter(socket);

    const gen = adapter.run(runOptions("hi")) as AsyncGenerator<any>;
    const runPromise = collectYields(gen, 2);
    await Promise.resolve();
    fake.receive({ type: "final", content: "hello there" });
    fake.receive({ type: "turn_complete" });

    const results = await runPromise;

    expect(results[0]!.content).toEqual([{ type: "text", text: "hello there" }]);
    const final = await gen.next();
    expect(final.done).toBe(true);
  });

  it("marks the yielded result as ask-user via metadata.custom", async () => {
    const fake = new FakeSocket();
    fake.open();
    const socket = createSessionSocket("s1", () => fake as unknown as WebSocket);
    const adapter = createWebSocketChatAdapter(socket);

    const gen = adapter.run(runOptions("hi")) as AsyncGenerator<any>;
    const runPromise = collectYields(gen, 1);
    await Promise.resolve();
    fake.receive({ type: "ask_user", prompt: "What is your name?" });

    const [result] = await runPromise;

    expect(result.content).toEqual([{ type: "text", text: "What is your name?" }]);
    expect(result.metadata).toEqual({ custom: { askUser: true } });

    const final = await gen.next();
    expect(final.done).toBe(true);
  });

  it("throws when the server reports an error", async () => {
    const fake = new FakeSocket();
    fake.open();
    const socket = createSessionSocket("s1", () => fake as unknown as WebSocket);
    const adapter = createWebSocketChatAdapter(socket);

    const gen = adapter.run(runOptions("hi")) as AsyncGenerator<any>;
    const nextPromise = gen.next();
    await Promise.resolve();
    fake.receive({ type: "error", message: "该助手当前不可用" });

    await expect(nextPromise).rejects.toThrow("该助手当前不可用");
  });
});
