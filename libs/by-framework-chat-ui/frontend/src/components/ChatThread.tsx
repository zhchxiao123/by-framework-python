import {
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useMessage,
} from "@assistant-ui/react";
import { Avatar } from "./Avatar";
import { ErrorNotice } from "./ErrorNotice";
import { MarkdownText } from "./MarkdownText";
import type { ConnectionStatus } from "../chatSocket";
import { formatTime } from "../formatTime";

function Timestamp() {
  const createdAt = useMessage((state) => state.createdAt);
  return (
    <span className="mt-1 block text-xs text-slate-400 dark:text-slate-500">
      {formatTime(createdAt)}
    </span>
  );
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="mb-2.5 flex items-end justify-end gap-2">
      <div className="max-w-[75%] text-right">
        <div className="rounded-2xl rounded-tr-sm bg-brand-500 px-3.5 py-2 text-left text-white shadow-sm">
          <MessagePrimitive.Parts />
        </div>
        <Timestamp />
      </div>
      <Avatar role="user" />
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  // Marked by chatAdapter.ts on a live `ask_user` event, or by
  // messageMapping.ts when reopening a Conversation whose persisted
  // `is_ask_user` flag was set — same marker, same styling either way, so
  // scrolling back through history still shows which messages were
  // clarifying questions.
  const isAskUser = useMessage(
    (state) => Boolean((state.metadata.custom as { askUser?: boolean })?.askUser),
  );
  // assistant-ui catches a thrown ChatModelAdapter error (chatAdapter.ts
  // throws on a `{type: "error"}` WS event — e.g. AGENT_UNAVAILABLE_MESSAGE)
  // and sets status to `{type: "incomplete", reason: "error"}`.
  const errorMessage = useMessage((state) =>
    state.status?.type === "incomplete" && state.status.reason === "error"
      ? state.status.error
      : null,
  );

  if (errorMessage !== null) {
    return (
      <MessagePrimitive.Root className="mb-2.5 flex items-end gap-2">
        <Avatar role="assistant" />
        <div className="max-w-[75%]">
          <ErrorNotice message={String(errorMessage)} />
          <Timestamp />
        </div>
      </MessagePrimitive.Root>
    );
  }

  return (
    <MessagePrimitive.Root className="mb-2.5 flex items-end gap-2">
      <Avatar role="assistant" />
      <div className="max-w-[75%]">
        {isAskUser && (
          <span className="mb-1 inline-block rounded-full bg-ask-500/10 px-2 py-0.5 text-xs font-medium text-ask-500 dark:bg-ask-500/20">
            需要你回复
          </span>
        )}
        <div
          className={
            isAskUser
              ? "rounded-2xl rounded-tl-sm border-l-4 border-ask-500 bg-ask-50 px-3.5 py-2 text-slate-800 shadow-sm dark:bg-ask-500/10 dark:text-slate-100"
              : "rounded-2xl rounded-tl-sm bg-white px-3.5 py-2 text-slate-800 shadow-sm ring-1 ring-slate-200 dark:bg-slate-800 dark:text-slate-100 dark:ring-slate-700"
          }
        >
          <MessagePrimitive.Parts components={{ Text: MarkdownText }} />
        </div>
        <Timestamp />
      </div>
    </MessagePrimitive.Root>
  );
}

export function ChatThread({
  locked,
  connectionStatus = "open",
}: {
  locked: boolean;
  connectionStatus?: ConnectionStatus;
}) {
  return (
    <ThreadPrimitive.Root className="flex h-full min-h-0 flex-col">
      {connectionStatus === "reconnecting" && (
        <div className="flex items-center gap-2 bg-amber-50 px-4 py-2 text-sm text-amber-700 dark:bg-amber-500/10 dark:text-amber-400">
          <span aria-hidden>⟳</span>
          重新连接中…
        </div>
      )}

      <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto px-4 py-3">
        <ThreadPrimitive.Empty>
          <p className="text-sm text-slate-400 dark:text-slate-500">开始对话吧…</p>
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages
          components={{ UserMessage, AssistantMessage }}
        />
      </ThreadPrimitive.Viewport>

      {locked && (
        <div className="flex items-center gap-2 border-t border-slate-200 bg-slate-100 px-4 py-1.5 text-sm text-slate-500 dark:border-slate-800 dark:bg-slate-800/60 dark:text-slate-400">
          <span aria-hidden>🔒</span>
          请等待当前回复完成
        </div>
      )}

      <ComposerPrimitive.Root className="flex items-center gap-2 border-t border-slate-200 bg-white px-3 py-2.5 dark:border-slate-800 dark:bg-slate-900">
        <ComposerPrimitive.Input
          placeholder="输入消息…"
          rows={1}
          disabled={locked}
          className="max-h-40 flex-1 resize-none rounded-full border border-slate-300 px-3.5 py-2 text-sm outline-none focus:border-brand-500 disabled:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:placeholder:text-slate-500 dark:disabled:bg-slate-800/50"
        />
        <ComposerPrimitive.Send
          disabled={locked}
          aria-label="发送"
          className="flex h-9 w-9 flex-none items-center justify-center rounded-full bg-brand-500 text-white hover:bg-brand-600 disabled:opacity-40"
        >
          <svg viewBox="0 0 24 24" fill="currentColor" className="h-4 w-4">
            <path d="M3.4 20.4l17.45-8.05a1 1 0 000-1.8L3.4 2.5a1 1 0 00-1.4 1.05L4.1 11 2 19.35a1 1 0 001.4 1.05z" />
          </svg>
        </ComposerPrimitive.Send>
      </ComposerPrimitive.Root>
    </ThreadPrimitive.Root>
  );
}
