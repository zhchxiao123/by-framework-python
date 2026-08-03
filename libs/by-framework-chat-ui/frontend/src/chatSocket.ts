import { isLockEvent, type ServerEvent } from "./protocol";

export type SocketFactory = (sessionId: string) => WebSocket;

export function defaultSocketFactory(sessionId: string): WebSocket {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(`${protocol}//${window.location.host}/ws/conversations/${sessionId}`);
}

export type ConnectionStatus = "connecting" | "open" | "reconnecting";

const INITIAL_RECONNECT_DELAY_MS = 500;
const MAX_RECONNECT_DELAY_MS = 8000;

export interface SessionSocket {
  waitForOpen(): Promise<void>;
  send(content: string): void;
  /**
   * Events for the turn currently in flight (chunk/final/ask_user/turn_complete/error/other).
   * Only one call's returned generator is meant to be actively consumed at
   * a time per socket — starting a new one discards any events left over
   * from a previous, abandoned call.
   */
  nextTurnEvents(signal: AbortSignal): AsyncGenerator<ServerEvent>;
  /** Lock-state broadcasts, delivered regardless of which tab/turn triggered them (#114). */
  onLockEvent(listener: (locked: boolean) => void): () => void;
  /** Replays the current status immediately, then notifies on every change. */
  onConnectionStatusChange(listener: (status: ConnectionStatus) => void): () => void;
  close(): void;
}

/**
 * Wraps one logical WebSocket connection for the life of a Conversation.
 *
 * A single socket is shared across every `run()` call the ChatModelAdapter
 * makes for this Conversation: incoming events are demultiplexed here so
 * that lock/unlocked broadcasts (which can arrive even when this tab isn't
 * the one sending) are never dropped just because no turn is in flight.
 *
 * An unintentional close (dropped connection, not our own `close()` call)
 * triggers an automatic reconnect with exponential backoff, transparently
 * swapping in a new underlying WebSocket — `onConnectionStatusChange` is how
 * the UI observes that this is happening.
 */
export function createSessionSocket(
  sessionId: string,
  factory: SocketFactory = defaultSocketFactory,
): SessionSocket {
  const lockListeners = new Set<(locked: boolean) => void>();
  const statusListeners = new Set<(status: ConnectionStatus) => void>();
  const turnQueue: ServerEvent[] = [];
  let wakeTurnWaiter: (() => void) | null = null;
  let socketClosed = false;
  let intentionalClose = false;
  let reconnectDelayMs = INITIAL_RECONNECT_DELAY_MS;
  let currentStatus: ConnectionStatus = "connecting";
  let ws: WebSocket;

  function setStatus(status: ConnectionStatus) {
    currentStatus = status;
    statusListeners.forEach((listener) => listener(status));
  }

  function wake() {
    if (wakeTurnWaiter) {
      const resolve = wakeTurnWaiter;
      wakeTurnWaiter = null;
      resolve();
    }
  }

  function dispatch(event: ServerEvent) {
    if (isLockEvent(event)) {
      const locked = event.type === "locked";
      lockListeners.forEach((listener) => listener(locked));
      return;
    }
    turnQueue.push(event);
    wake();
  }

  function attach(socket: WebSocket) {
    socket.addEventListener("message", (rawEvent) => {
      const messageEvent = rawEvent as MessageEvent<string>;
      try {
        dispatch(JSON.parse(messageEvent.data) as ServerEvent);
      } catch {
        dispatch({ type: "other" });
      }
    });
    socket.addEventListener("open", () => {
      reconnectDelayMs = INITIAL_RECONNECT_DELAY_MS;
      setStatus("open");
    });
    socket.addEventListener("close", () => {
      if (intentionalClose) {
        socketClosed = true;
        wake();
        return;
      }
      setStatus("reconnecting");
      setTimeout(() => {
        ws = factory(sessionId);
        attach(ws);
      }, reconnectDelayMs);
      reconnectDelayMs = Math.min(reconnectDelayMs * 2, MAX_RECONNECT_DELAY_MS);
    });
  }

  ws = factory(sessionId);
  attach(ws);

  return {
    waitForOpen() {
      if (ws.readyState === WebSocket.OPEN) return Promise.resolve();
      return new Promise((resolve) => {
        const onOpen = () => {
          ws.removeEventListener("open", onOpen);
          resolve();
        };
        ws.addEventListener("open", onOpen);
      });
    },

    send(content: string) {
      // Starting a new turn: anything still queued belongs to a previous
      // turn that was aborted before draining (the server doesn't know a
      // turn was abandoned client-side and keeps pushing its remaining
      // chunk/final/turn_complete events regardless). Discarding here,
      // rather than in `nextTurnEvents`, is the one unambiguous point
      // "before this call, nothing in the queue can possibly be for the
      // turn we're about to start" — the server hasn't even seen this
      // message yet.
      turnQueue.length = 0;
      ws.send(JSON.stringify({ content }));
    },

    async *nextTurnEvents(signal: AbortSignal) {
      // `signal.aborted` is checked directly (not just via the "abort"
      // listener) because async generators are lazy: if the caller aborts
      // before the first `.next()` call, the listener attached here would
      // never see an event that already fired.
      const onAbort = () => wake();
      signal.addEventListener("abort", onAbort);
      try {
        while (true) {
          if (turnQueue.length > 0) {
            yield turnQueue.shift()!;
            continue;
          }
          if (signal.aborted || socketClosed) return;
          await new Promise<void>((resolve) => {
            wakeTurnWaiter = resolve;
          });
        }
      } finally {
        signal.removeEventListener("abort", onAbort);
      }
    },

    onLockEvent(listener: (locked: boolean) => void) {
      lockListeners.add(listener);
      return () => lockListeners.delete(listener);
    },

    onConnectionStatusChange(listener: (status: ConnectionStatus) => void) {
      listener(currentStatus);
      statusListeners.add(listener);
      return () => statusListeners.delete(listener);
    },

    close() {
      intentionalClose = true;
      ws.close();
    },
  };
}
