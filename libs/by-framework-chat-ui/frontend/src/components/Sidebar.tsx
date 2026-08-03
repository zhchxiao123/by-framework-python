import { useEffect, useState } from "react";
import { listConversations, type ConversationSummary } from "../api";

export interface SidebarProps {
  selectedSessionId: string | null;
  onSelect: (sessionId: string) => void;
  onNewConversation: () => void;
  /** Bumped by the parent to force a re-fetch (e.g. after starting a conversation). */
  refreshKey?: number;
}

export function Sidebar({
  selectedSessionId,
  onSelect,
  onNewConversation,
  refreshKey,
}: SidebarProps) {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);

  useEffect(() => {
    let cancelled = false;
    listConversations().then((items) => {
      if (!cancelled) setConversations(items);
    });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  return (
    <div className="flex h-full w-64 flex-col gap-3 border-r border-slate-200 bg-slate-50 p-4">
      <h2 className="text-sm font-semibold text-slate-800">历史对话</h2>
      <button
        type="button"
        onClick={onNewConversation}
        className="rounded-xl bg-brand-500 px-3 py-2 text-sm font-medium text-white hover:bg-brand-600"
      >
        + 新对话
      </button>
      <ul className="flex flex-col gap-1.5 overflow-y-auto">
        {conversations.map((conversation) => {
          const isSelected = conversation.session_id === selectedSessionId;
          return (
            <li key={conversation.session_id}>
              <button
                type="button"
                data-testid={`conversation-${conversation.session_id}`}
                onClick={() => onSelect(conversation.session_id)}
                className={
                  isSelected
                    ? "selected w-full rounded-lg border-l-4 border-brand-500 bg-brand-50 px-3 py-2 text-left text-sm text-slate-800"
                    : "w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-left text-sm text-slate-700 hover:border-brand-500"
                }
              >
                {(conversation.title || "(新对话)") + " · " + conversation.agent_type}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
