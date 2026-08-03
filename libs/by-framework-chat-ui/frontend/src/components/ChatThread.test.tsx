import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AssistantRuntimeProvider, useLocalRuntime } from "@assistant-ui/react";
import type { ConnectionStatus } from "../chatSocket";
import { ChatThread } from "./ChatThread";
import { historyMessageToThreadMessage } from "../messageMapping";
import { FakeSocket } from "../test-utils/fakeSocket";
import { createWebSocketChatAdapter } from "../chatAdapter";
import { createSessionSocket } from "../chatSocket";

function Fixture({
  locked = false,
  connectionStatus = "open",
}: {
  locked?: boolean;
  connectionStatus?: ConnectionStatus;
}) {
  const socket = createSessionSocket("s1", () => new FakeSocket() as unknown as WebSocket);
  const adapter = createWebSocketChatAdapter(socket);
  const initialMessages = [
    historyMessageToThreadMessage(
      {
        role: "user",
        content: "你好",
        is_ask_user: false,
        created_at: "2026-08-03T09:05:00+00:00",
      },
      0,
    ),
    historyMessageToThreadMessage(
      {
        role: "assistant",
        content: "好的,请问查询哪个城市?",
        is_ask_user: false,
        created_at: "2026-08-03T09:06:00+00:00",
      },
      1,
    ),
    historyMessageToThreadMessage(
      {
        role: "assistant",
        content: "可以告诉我你的名字吗?",
        is_ask_user: true,
        created_at: "2026-08-03T09:07:00+00:00",
      },
      2,
    ),
  ];
  const runtime = useLocalRuntime(adapter, { initialMessages });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ChatThread locked={locked} connectionStatus={connectionStatus} />
    </AssistantRuntimeProvider>
  );
}

function ErrorFixture({ socket }: { socket: ReturnType<typeof createSessionSocket> }) {
  const adapter = createWebSocketChatAdapter(socket);
  const runtime = useLocalRuntime(adapter);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ChatThread locked={false} />
    </AssistantRuntimeProvider>
  );
}

describe("ChatThread ask-user bubble", () => {
  it("shows the ask-user badge only on the message marked askUser", () => {
    render(<Fixture />);

    const badges = screen.getAllByText("需要你回复");
    expect(badges).toHaveLength(1);

    expect(screen.getByText("可以告诉我你的名字吗?")).toBeInTheDocument();
    expect(screen.getByText("好的,请问查询哪个城市?")).toBeInTheDocument();
  });
});

describe("ChatThread locked state", () => {
  it("shows the locked banner and disables the composer when locked", () => {
    render(<Fixture locked />);

    expect(screen.getByText("请等待当前回复完成")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("输入消息…")).toBeDisabled();
  });

  it("hides the banner and enables the composer when unlocked", () => {
    render(<Fixture locked={false} />);

    expect(screen.queryByText("请等待当前回复完成")).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("输入消息…")).not.toBeDisabled();
  });
});

describe("ChatThread connection status", () => {
  it("shows the reconnecting banner only when connectionStatus is reconnecting", () => {
    const { rerender } = render(<Fixture connectionStatus="open" />);
    expect(screen.queryByText("重新连接中…")).not.toBeInTheDocument();

    rerender(<Fixture connectionStatus="reconnecting" />);
    expect(screen.getByText("重新连接中…")).toBeInTheDocument();
  });
});

describe("ChatThread adapter errors", () => {
  it("renders a thrown adapter error as a styled ErrorNotice, not raw text", async () => {
    const userEvent = (await import("@testing-library/user-event")).default;
    const fake = new FakeSocket();
    fake.open();
    const socket = createSessionSocket("s1", () => fake as unknown as WebSocket);

    render(<ErrorFixture socket={socket} />);

    const input = screen.getByPlaceholderText("输入消息…");
    await userEvent.type(input, "hi{Enter}");
    fake.receive({ type: "error", message: "该助手当前不可用" });

    const notice = await screen.findByText("该助手当前不可用");
    expect(notice.className).toContain("text-red-700");
  });
});
