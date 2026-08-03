import {
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useMessage,
} from "@assistant-ui/react";
import { ErrorNotice } from "./ErrorNotice";
import type { ConnectionStatus } from "../chatSocket";

function UserMessage() {
  return (
    <MessagePrimitive.Root className="flex justify-end mb-3">
      <div className="max-w-[70%] rounded-2xl rounded-tr-sm bg-brand-500 px-4 py-2.5 text-white shadow-sm">
        <MessagePrimitive.Parts />
      </div>
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
      <MessagePrimitive.Root className="flex justify-start mb-3">
        <ErrorNotice message={String(errorMessage)} />
      </MessagePrimitive.Root>
    );
  }

  return (
    <MessagePrimitive.Root className="flex justify-start mb-3">
      <div
        className={
          isAskUser
            ? "max-w-[70%] rounded-2xl rounded-tl-sm border-l-4 border-ask-500 bg-ask-50 px-4 py-2.5 text-slate-800 shadow-sm"
            : "max-w-[70%] rounded-2xl rounded-tl-sm bg-white px-4 py-2.5 text-slate-800 shadow-sm ring-1 ring-slate-200"
        }
      >
        {isAskUser && (
          <span className="mb-1 inline-block rounded-full bg-ask-500/10 px-2 py-0.5 text-xs font-medium text-ask-500">
            需要你回复
          </span>
        )}
        <MessagePrimitive.Parts />
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
    <ThreadPrimitive.Root className="flex h-full flex-col">
      {connectionStatus === "reconnecting" && (
        <div className="flex items-center gap-2 bg-amber-50 px-4 py-2 text-sm text-amber-700">
          <span aria-hidden>⟳</span>
          重新连接中…
        </div>
      )}

      <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto px-6 py-4">
        <ThreadPrimitive.Empty>
          <p className="text-sm text-slate-400">开始对话吧…</p>
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages
          components={{ UserMessage, AssistantMessage }}
        />
      </ThreadPrimitive.Viewport>

      {locked && (
        <div className="flex items-center gap-2 border-t border-slate-200 bg-slate-100 px-4 py-2 text-sm text-slate-500">
          <span aria-hidden>🔒</span>
          请等待当前回复完成
        </div>
      )}

      <ComposerPrimitive.Root className="flex items-center gap-2 border-t border-slate-200 bg-white px-4 py-3">
        <ComposerPrimitive.Input
          placeholder="输入消息…"
          rows={1}
          disabled={locked}
          className="max-h-40 flex-1 resize-none rounded-xl border border-slate-300 px-4 py-2.5 text-sm outline-none focus:border-brand-500 disabled:bg-slate-50"
        />
        <ComposerPrimitive.Send
          disabled={locked}
          className="rounded-xl bg-brand-500 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-600 disabled:opacity-40"
        >
          发送
        </ComposerPrimitive.Send>
      </ComposerPrimitive.Root>
    </ThreadPrimitive.Root>
  );
}
