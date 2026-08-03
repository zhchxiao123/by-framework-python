import { useEffect, useMemo, useState } from "react";
import { AssistantRuntimeProvider, useLocalRuntime, type ThreadMessage } from "@assistant-ui/react";
import { AssistantPicker } from "./components/AssistantPicker";
import { ChatThread } from "./components/ChatThread";
import { ErrorNotice } from "./components/ErrorNotice";
import { Sidebar } from "./components/Sidebar";
import { createWebSocketChatAdapter } from "./chatAdapter";
import { createSessionSocket } from "./chatSocket";
import { createConversation, getConversation } from "./api";
import { historyMessageToThreadMessage } from "./messageMapping";
import { useLockState } from "./useLockState";
import { useConnectionStatus } from "./useConnectionStatus";

interface OpenConversation {
  sessionId: string;
  agentType: string;
  initialMessages: ThreadMessage[];
}

function Chat({ conversation }: { conversation: OpenConversation }) {
  // One WebSocket connection per Conversation, shared between the adapter
  // (turn events) and the lock-state hook (locked/unlocked broadcasts) —
  // see chatAdapter.ts's docstring. Closed on unmount/conversation-switch
  // (below) so its reconnect-with-backoff loop doesn't keep running for an
  // abandoned Conversation.
  const socket = useMemo(
    () => createSessionSocket(conversation.sessionId),
    [conversation.sessionId],
  );
  useEffect(() => {
    return () => socket.close();
  }, [socket]);
  const adapter = useMemo(() => createWebSocketChatAdapter(socket), [socket]);
  const runtime = useLocalRuntime(adapter, {
    initialMessages: conversation.initialMessages,
  });
  const locked = useLockState(socket);
  const connectionStatus = useConnectionStatus(socket);

  return (
    <div className="flex h-full flex-1 flex-col">
      <div className="border-b border-slate-200 bg-white px-6 py-4">
        <p className="text-sm">
          <span className="font-semibold text-slate-800">助手:</span>{" "}
          <span className="text-slate-600">{conversation.agentType}</span>
        </p>
      </div>
      <AssistantRuntimeProvider runtime={runtime}>
        <ChatThread locked={locked} connectionStatus={connectionStatus} />
      </AssistantRuntimeProvider>
    </div>
  );
}

export function App() {
  const [conversation, setConversation] = useState<OpenConversation | null>(null);
  const [sidebarRefreshKey, setSidebarRefreshKey] = useState(0);
  const [error, setError] = useState<string | null>(null);

  async function startConversation(agentType: string) {
    setError(null);
    try {
      const created = await createConversation(agentType);
      setConversation({
        sessionId: created.session_id,
        agentType: created.agent_type,
        initialMessages: [],
      });
      setSidebarRefreshKey((key) => key + 1);
    } catch {
      setError("该助手当前不可用");
    }
  }

  async function openConversation(sessionId: string) {
    setError(null);
    try {
      const detail = await getConversation(sessionId);
      setConversation({
        sessionId: detail.session_id,
        agentType: detail.agent_type,
        initialMessages: detail.messages.map(historyMessageToThreadMessage),
      });
    } catch {
      setError("conversation not found");
    }
  }

  function goToPicker() {
    setConversation(null);
    setError(null);
    setSidebarRefreshKey((key) => key + 1);
  }

  return (
    <div className="flex h-screen w-screen bg-slate-50">
      <Sidebar
        selectedSessionId={conversation?.sessionId ?? null}
        onSelect={openConversation}
        onNewConversation={goToPicker}
        refreshKey={sidebarRefreshKey}
      />
      <div className="flex flex-1 flex-col">
        {error !== null && (
          <div className="p-4">
            <ErrorNotice message={error} />
          </div>
        )}
        {conversation === null ? (
          <AssistantPicker onPick={startConversation} />
        ) : (
          <Chat key={conversation.sessionId} conversation={conversation} />
        )}
      </div>
    </div>
  );
}
