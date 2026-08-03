import { describe, expect, it } from "vitest";
import { applyServerEvent, initialTurnState } from "./protocol";

describe("applyServerEvent", () => {
  it("accumulates chunk content", () => {
    let state = initialTurnState();
    state = applyServerEvent(state, { type: "chunk", content: "hel" });
    state = applyServerEvent(state, { type: "chunk", content: "lo" });

    expect(state.accumulated).toBe("hello");
    expect(state.done).toBe(false);
  });

  it("replaces accumulated text with the authoritative final content", () => {
    let state = initialTurnState();
    state = applyServerEvent(state, { type: "chunk", content: "hel" });
    state = applyServerEvent(state, { type: "final", content: "hello there" });

    expect(state.accumulated).toBe("hello there");
  });

  it("marks turn_complete as done without setting isAskUser", () => {
    let state = initialTurnState();
    state = applyServerEvent(state, { type: "final", content: "hello" });
    state = applyServerEvent(state, { type: "turn_complete" });

    expect(state.done).toBe(true);
    expect(state.isAskUser).toBe(false);
  });

  it("marks ask_user as done and isAskUser, using the prompt as content", () => {
    let state = initialTurnState();
    state = applyServerEvent(state, { type: "ask_user", prompt: "What is your name?" });

    expect(state.done).toBe(true);
    expect(state.isAskUser).toBe(true);
    expect(state.accumulated).toBe("What is your name?");
  });

  it("captures an error and marks the turn done", () => {
    let state = initialTurnState();
    state = applyServerEvent(state, { type: "error", message: "该助手当前不可用" });

    expect(state.done).toBe(true);
    expect(state.error).toBe("该助手当前不可用");
  });

  it("ignores locked/unlocked/other events for turn content", () => {
    let state = initialTurnState();
    state = applyServerEvent(state, { type: "chunk", content: "hi" });
    const beforeLock = state;
    state = applyServerEvent(state, { type: "locked" });

    expect(state).toEqual(beforeLock);
  });

  it("adds a tool_call event as a pending tool call entry", () => {
    let state = initialTurnState();
    state = applyServerEvent(state, {
      type: "tool_call",
      call_id: "call_1",
      name: "calculate",
      arguments: '{"expression": "1+1"}',
    });

    expect(state.toolCalls).toEqual([
      { toolCallId: "call_1", toolName: "calculate", argsText: '{"expression": "1+1"}' },
    ]);
  });

  it("fills in the result of the matching tool call on tool_result", () => {
    let state = initialTurnState();
    state = applyServerEvent(state, {
      type: "tool_call",
      call_id: "call_1",
      name: "calculate",
      arguments: "{}",
    });
    state = applyServerEvent(state, {
      type: "tool_result",
      call_id: "call_1",
      content: "2",
      tool_name: "calculate",
    });

    expect(state.toolCalls).toEqual([
      { toolCallId: "call_1", toolName: "calculate", argsText: "{}", result: "2" },
    ]);
  });

  it("renders a placeholder tool call for a tool_result with no matching prior tool_call", () => {
    let state = initialTurnState();
    state = applyServerEvent(state, {
      type: "tool_result",
      call_id: "call_orphan",
      content: "2",
      tool_name: "calculate",
    });

    expect(state.toolCalls).toEqual([
      { toolCallId: "call_orphan", toolName: "calculate", argsText: "", result: "2" },
    ]);
  });
});
