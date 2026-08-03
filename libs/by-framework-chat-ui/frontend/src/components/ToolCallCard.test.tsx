import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ToolCallCard } from "./ToolCallCard";

function baseProps(overrides: Partial<Parameters<typeof ToolCallCard>[0]> = {}) {
  return {
    type: "tool-call" as const,
    toolCallId: "call_1",
    toolName: "calculate",
    args: { expression: "1+1" },
    argsText: '{"expression": "1+1"}',
    status: { type: "complete" as const },
    addResult: () => {},
    ...overrides,
  };
}

describe("ToolCallCard", () => {
  it("shows the tool name and pretty-printed arguments", () => {
    render(<ToolCallCard {...baseProps()} />);

    expect(screen.getByText(/调用了 calculate/)).toBeInTheDocument();
    expect(screen.getByText(/"expression": "1\+1"/)).toBeInTheDocument();
  });

  it("shows a running placeholder and no result section before the result arrives", () => {
    render(<ToolCallCard {...baseProps()} />);

    expect(screen.getByText("运行中…")).toBeInTheDocument();
    expect(screen.queryByText("结果")).not.toBeInTheDocument();
  });

  it("shows the result and hides the running placeholder once it arrives", () => {
    render(<ToolCallCard {...baseProps({ result: "2" })} />);

    expect(screen.queryByText("运行中…")).not.toBeInTheDocument();
    expect(screen.getByText("结果")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("falls back to a generic label when toolName is empty", () => {
    render(<ToolCallCard {...baseProps({ toolName: "" })} />);

    expect(screen.getByText(/调用了 工具/)).toBeInTheDocument();
  });

  it("is collapsed by default, expandable via the summary", () => {
    const { container } = render(<ToolCallCard {...baseProps({ result: "2" })} />);

    const details = container.querySelector("details");
    expect(details?.open).toBe(false);
  });
});
