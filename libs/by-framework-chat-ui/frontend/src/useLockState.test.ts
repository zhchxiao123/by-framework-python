import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useLockState } from "./useLockState";
import { createSessionSocket } from "./chatSocket";
import { FakeSocket } from "./test-utils/fakeSocket";

describe("useLockState", () => {
  it("starts unlocked and reflects locked/unlocked broadcasts", () => {
    const fake = new FakeSocket();
    const socket = createSessionSocket("s1", () => fake as unknown as WebSocket);
    const { result } = renderHook(() => useLockState(socket));

    expect(result.current).toBe(false);

    act(() => {
      fake.receive({ type: "locked" });
    });
    expect(result.current).toBe(true);

    act(() => {
      fake.receive({ type: "unlocked" });
    });
    expect(result.current).toBe(false);
  });

  it("unsubscribes on unmount", () => {
    const fake = new FakeSocket();
    const socket = createSessionSocket("s1", () => fake as unknown as WebSocket);
    const { result, unmount } = renderHook(() => useLockState(socket));

    unmount();

    expect(() => fake.receive({ type: "locked" })).not.toThrow();
    expect(result.current).toBe(false);
  });
});
