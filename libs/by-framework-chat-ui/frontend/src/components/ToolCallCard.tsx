import type { ToolCallMessagePartProps } from "@assistant-ui/react";

function formatArgs(argsText: string): string {
  try {
    return JSON.stringify(JSON.parse(argsText), null, 2);
  } catch {
    return argsText;
  }
}

export function ToolCallCard({ toolName, argsText, result }: ToolCallMessagePartProps) {
  const hasResult = result !== undefined && result !== null;
  return (
    <details className="my-1.5 rounded-xl border border-slate-200 bg-slate-50 text-xs text-slate-600 open:pb-2 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-300">
      <summary className="flex cursor-pointer items-center gap-1.5 px-3 py-2 font-medium text-slate-700 dark:text-slate-200">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-3.5 w-3.5 flex-none">
          <path d="M14.7 6.3a1 1 0 010 1.4L9.4 13l1.3 1.3-5.7 2 2-5.7L8.3 12l5.3-5.3a1 1 0 011.1-.4zM17 3l1.5 1.5L20 3l1 1-1.5 1.5L21 7l-1 1-1.5-1.5L17 8l-1-1 1.5-1.5L16 4z" />
        </svg>
        调用了 {toolName || "工具"}
        {!hasResult && <span className="text-slate-400 dark:text-slate-500">运行中…</span>}
      </summary>
      <div className="space-y-1.5 px-3">
        <div>
          <p className="mb-0.5 text-slate-400 dark:text-slate-500">参数</p>
          <pre className="overflow-x-auto rounded-lg bg-slate-900/[0.06] p-2 dark:bg-white/10">
            {formatArgs(argsText)}
          </pre>
        </div>
        {hasResult && (
          <div>
            <p className="mb-0.5 text-slate-400 dark:text-slate-500">结果</p>
            <pre className="overflow-x-auto rounded-lg bg-slate-900/[0.06] p-2 dark:bg-white/10">
              {String(result)}
            </pre>
          </div>
        )}
      </div>
    </details>
  );
}
