import { describe, expect, it, vi } from "vitest";
import { createSessionSocket } from "./chatSocket";
import { FakeSocket } from "./test-utils/fakeSocket";

describe("createSessionSocket", () => {
  it("waits for the socket to open before sending", async () => {
    const fake = new FakeSocket();
    const session = createSessionSocket("s1", () => fake as unknown as WebSocket);

    const sendPromise = session.waitForOpen().then(() => session.send("hi"));
    expect(fake.sent).toEqual([]);

    fake.open();
    await sendPromise;

    expect(fake.sent).toEqual([JSON.stringify({ content: "hi" })]);
  });

  it("sends immediately if the socket is already open", async () => {
    const fake = new FakeSocket();
    fake.open();
    const session = createSessionSocket("s1", () => fake as unknown as WebSocket);

    await session.waitForOpen();
    session.send("hi");

    expect(fake.sent).toEqual([JSON.stringify({ content: "hi" })]);
  });

  it("surfaces chunk/final events via nextTurnEvents, in order", async () => {
    // The generator doesn't self-terminate on turn_complete — like the
    // backend's stream_turn, it forwards every event and leaves the caller
    // to decide when a turn is over (it only stops itself on abort/close).
    const fake = new FakeSocket();
    fake.open();
    const session = createSessionSocket("s1", () => fake as unknown as WebSocket);
    const controller = new AbortController();
    const iterator = session.nextTurnEvents(controller.signal);

    fake.receive({ type: "chunk", content: "hel" });
    fake.receive({ type: "chunk", content: "lo" });
    fake.receive({ type: "turn_complete" });

    const events = [];
    for await (const event of iterator) {
      events.push(event);
      if (event.type === "turn_complete") break;
    }

    expect(events).toEqual([
      { type: "chunk", content: "hel" },
      { type: "chunk", content: "lo" },
      { type: "turn_complete" },
    ]);
  });

  it("routes locked/unlocked events to lock listeners, not nextTurnEvents", async () => {
    const fake = new FakeSocket();
    fake.open();
    const session = createSessionSocket("s1", () => fake as unknown as WebSocket);
    const onLock = vi.fn();
    session.onLockEvent(onLock);

    const controller = new AbortController();
    const iterator = session.nextTurnEvents(controller.signal);

    fake.receive({ type: "locked" });
    fake.receive({ type: "chunk", content: "hi" });
    fake.receive({ type: "unlocked" });
    fake.receive({ type: "turn_complete" });

    const events = [];
    for await (const event of iterator) {
      events.push(event);
      if (event.type === "turn_complete") break;
    }

    expect(events).toEqual([{ type: "chunk", content: "hi" }, { type: "turn_complete" }]);
    expect(onLock.mock.calls).toEqual([[true], [false]]);
  });

  it("stops nextTurnEvents when the abort signal fires", async () => {
    const fake = new FakeSocket();
    fake.open();
    const session = createSessionSocket("s1", () => fake as unknown as WebSocket);
    const controller = new AbortController();
    const iterator = session.nextTurnEvents(controller.signal);

    fake.receive({ type: "chunk", content: "hi" });
    controller.abort();

    const events = [];
    for await (const event of iterator) {
      events.push(event);
    }

    expect(events).toEqual([{ type: "chunk", content: "hi" }]);
  });

  it("unsubscribing a lock listener stops further notifications", async () => {
    const fake = new FakeSocket();
    fake.open();
    const session = createSessionSocket("s1", () => fake as unknown as WebSocket);
    const onLock = vi.fn();
    const unsubscribe = session.onLockEvent(onLock);
    unsubscribe();

    fake.receive({ type: "locked" });
    await Promise.resolve();

    expect(onLock).not.toHaveBeenCalled();
  });
});

describe("createSessionSocket connection status + reconnect", () => {
  it("reports connecting then open", async () => {
    const fake = new FakeSocket();
    const statuses: string[] = [];
    const session = createSessionSocket("s1", () => fake as unknown as WebSocket);
    session.onConnectionStatusChange((status) => statuses.push(status));

    fake.open();

    expect(statuses).toEqual(["connecting", "open"]);
  });

  it("reconnects with a new socket after an unintentional close, reporting reconnecting", async () => {
    const sockets: FakeSocket[] = [];
    const factory = () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket as unknown as WebSocket;
    };
    const statuses: string[] = [];
    const session = createSessionSocket("s1", factory);
    session.onConnectionStatusChange((status) => statuses.push(status));

    sockets[0]!.open();
    sockets[0]!.dispatchEvent(new Event("close")); // connection dropped unexpectedly

    expect(statuses).toEqual(["connecting", "open", "reconnecting"]);
    expect(sockets).toHaveLength(1); // the replacement socket is created after a backoff delay
  });

  it("does not report reconnecting after an intentional close()", async () => {
    const fake = new FakeSocket();
    const statuses: string[] = [];
    const session = createSessionSocket("s1", () => fake as unknown as WebSocket);
    session.onConnectionStatusChange((status) => statuses.push(status));

    fake.open();
    session.close();

    expect(statuses).toEqual(["connecting", "open"]);
  });
});

describe("createSessionSocket stale-turn protection", () => {
  it("discards events left over from an aborted previous turn once a new turn is sent", async () => {
    const fake = new FakeSocket();
    fake.open();
    const session = createSessionSocket("s1", () => fake as unknown as WebSocket);

    // Turn A is sent, then abandoned (aborted) mid-stream — before it
    // finished draining, so its leftover event stays queued.
    session.send("turn A content");
    const controllerA = new AbortController();
    controllerA.abort();

    // The server didn't know turn A was abandoned and keeps sending for it.
    fake.receive({ type: "chunk", content: "stale from turn A" });

    // Turn B is sent — this must discard turn A's leftover event.
    session.send("turn B content");
    fake.receive({ type: "final", content: "turn B" });
    fake.receive({ type: "turn_complete" });

    const controllerB = new AbortController();
    const eventsB: unknown[] = [];
    for await (const event of session.nextTurnEvents(controllerB.signal)) {
      eventsB.push(event);
      if (event.type === "turn_complete") break;
    }

    expect(eventsB).toEqual([
      { type: "final", content: "turn B" },
      { type: "turn_complete" },
    ]);
  });
});
