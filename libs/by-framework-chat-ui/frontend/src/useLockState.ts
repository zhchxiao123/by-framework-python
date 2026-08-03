import { useEffect, useState } from "react";
import type { SessionSocket } from "./chatSocket";

/**
 * Live lock state for a Conversation (#114). Subscribes to the same
 * `SessionSocket` the adapter uses, so a "locked"/"unlocked" broadcast is
 * seen even when this tab isn't the one that sent the in-flight message.
 */
export function useLockState(socket: SessionSocket): boolean {
  const [locked, setLocked] = useState(false);

  useEffect(() => {
    return socket.onLockEvent(setLocked);
  }, [socket]);

  return locked;
}
