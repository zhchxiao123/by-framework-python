import { useEffect, useState } from "react";
import { listConversations, type ConversationSummary } from "../api";
import { ThemeToggle } from "./ThemeToggle";

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
    <div className="flex h-full w-64 flex-col gap-3 border-r border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-950">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">历史对话</h2>
        <ThemeToggle />
      </div>
      <button
        type="button"
        onClick={onNewConversation}
        className="rounded-xl bg-brand-500 px-3 py-2.5 text-sm font-medium text-white shadow-sm hover:bg-brand-600"
      >
        + 新对话
      </button>
      <ul className="flex flex-col gap-2 overflow-y-auto">
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
                    ? "selected w-full rounded-xl border-l-4 border-brand-500 bg-brand-50 px-3 py-2.5 text-left text-sm text-slate-800 shadow-sm dark:bg-brand-500/10 dark:text-slate-100"
                    : "w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-left text-sm text-slate-700 hover:border-brand-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300 dark:hover:border-brand-500"
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
