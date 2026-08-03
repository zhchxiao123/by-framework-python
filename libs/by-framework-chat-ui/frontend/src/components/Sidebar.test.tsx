import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";
import type { ConversationSummary } from "../api";

const conversations: ConversationSummary[] = [
  { session_id: "s1", agent_type: "planner", title: "帮我写一份周报", last_active_at: null },
  { session_id: "s2", agent_type: "demo-assistant", title: "新对话", last_active_at: null },
];

describe("Sidebar", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ conversations }), { status: 200 }),
      ),
    );
  });

  it("renders every conversation with its title and agent_type", async () => {
    render(<Sidebar selectedSessionId={null} onSelect={vi.fn()} onNewConversation={vi.fn()} />);

    expect(await screen.findByText(/帮我写一份周报/)).toBeInTheDocument();
    expect(screen.getByText(/新对话.*demo-assistant/)).toBeInTheDocument();
  });

  it("highlights the selected conversation", async () => {
    render(<Sidebar selectedSessionId="s1" onSelect={vi.fn()} onNewConversation={vi.fn()} />);

    const selected = await screen.findByTestId("conversation-s1");
    const other = screen.getByTestId("conversation-s2");

    expect(selected.className).toContain("selected");
    expect(other.className).not.toContain("selected");
  });

  it("calls onSelect with the session_id when a conversation is clicked", async () => {
    const onSelect = vi.fn();
    render(<Sidebar selectedSessionId={null} onSelect={onSelect} onNewConversation={vi.fn()} />);

    await userEvent.click(await screen.findByTestId("conversation-s1"));

    expect(onSelect).toHaveBeenCalledWith("s1");
  });

  it("calls onNewConversation when the new-conversation button is clicked", async () => {
    const onNewConversation = vi.fn();
    render(
      <Sidebar selectedSessionId={null} onSelect={vi.fn()} onNewConversation={onNewConversation} />,
    );

    await userEvent.click(screen.getByText("+ 新对话"));

    expect(onNewConversation).toHaveBeenCalled();
  });
});
