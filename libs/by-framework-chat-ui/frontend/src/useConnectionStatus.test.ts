import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useConnectionStatus } from "./useConnectionStatus";
import { createSessionSocket } from "./chatSocket";
import { FakeSocket } from "./test-utils/fakeSocket";

describe("useConnectionStatus", () => {
  it("starts at the socket's current status and updates on change", () => {
    const fake = new FakeSocket();
    const socket = createSessionSocket("s1", () => fake as unknown as WebSocket);
    const { result } = renderHook(() => useConnectionStatus(socket));

    expect(result.current).toBe("connecting");

    act(() => {
      fake.open();
    });
    expect(result.current).toBe("open");
  });
});
