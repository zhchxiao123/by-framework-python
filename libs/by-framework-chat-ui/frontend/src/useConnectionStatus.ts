import { useEffect, useState } from "react";
import type { ConnectionStatus, SessionSocket } from "./chatSocket";

/** Live connection status for a Conversation's WebSocket (#110/slice 5). */
export function useConnectionStatus(socket: SessionSocket): ConnectionStatus {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");

  useEffect(() => {
    return socket.onConnectionStatusChange(setStatus);
  }, [socket]);

  return status;
}
