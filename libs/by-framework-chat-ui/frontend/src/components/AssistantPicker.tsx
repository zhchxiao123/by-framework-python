import { useEffect, useState } from "react";
import { listAgentTypes } from "../api";

export function AssistantPicker({
  onPick,
}: {
  onPick: (agentType: string) => void;
}) {
  const [agentTypes, setAgentTypes] = useState<string[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    listAgentTypes().then((types) => {
      if (!cancelled) setAgentTypes(types);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 px-6">
      <h1 className="text-lg font-semibold text-slate-800">选择一个助手开始对话</h1>
      <div className="flex w-full max-w-sm flex-col gap-2">
        {agentTypes === null && <p className="text-sm text-slate-400">加载中…</p>}
        {agentTypes?.length === 0 && (
          <p className="text-sm text-slate-400">当前没有在线的助手</p>
        )}
        {agentTypes?.map((agentType) => (
          <button
            key={agentType}
            type="button"
            onClick={() => onPick(agentType)}
            className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-left text-sm font-medium text-slate-700 shadow-sm hover:border-brand-500 hover:text-brand-600"
          >
            {agentType}
          </button>
        ))}
      </div>
    </div>
  );
}
