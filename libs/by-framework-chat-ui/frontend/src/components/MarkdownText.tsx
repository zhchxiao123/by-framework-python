import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";
import { memo } from "react";
import remarkGfm from "remark-gfm";

function MarkdownTextImpl() {
  return <MarkdownTextPrimitive remarkPlugins={[remarkGfm]} className="markdown-body" />;
}

export const MarkdownText = memo(MarkdownTextImpl);
