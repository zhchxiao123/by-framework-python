import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

describe("App", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/agents")) {
          return new Response(JSON.stringify({ agent_types: ["planner"] }), {
            status: 200,
          });
        }
        if (url.endsWith("/api/conversations")) {
          return new Response(JSON.stringify({ conversations: [] }), { status: 200 });
        }
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the assistant picker with the online agent types", async () => {
    render(<App />);

    expect(await screen.findByText("选择一个助手开始对话")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("planner")).toBeInTheDocument());
  });
});

describe("App: reopening a past conversation", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/agents")) {
          return new Response(JSON.stringify({ agent_types: ["planner"] }), {
            status: 200,
          });
        }
        if (url.endsWith("/api/conversations")) {
          return new Response(
            JSON.stringify({
              conversations: [
                {
                  session_id: "s1",
                  agent_type: "planner",
                  title: "帮我写一份周报",
                  last_active_at: null,
                },
                {
                  session_id: "s2",
                  agent_type: "demo-assistant",
                  title: "整理会议纪要",
                  last_active_at: null,
                },
              ],
            }),
            { status: 200 },
          );
        }
        if (url.endsWith("/api/conversations/s1")) {
          return new Response(
            JSON.stringify({
              session_id: "s1",
              agent_type: "planner",
              messages: [
                {
                  role: "user",
                  content: "你好",
                  is_ask_user: false,
                  created_at: "2026-08-03T09:05:00+00:00",
                },
              ],
            }),
            { status: 200 },
          );
        }
        if (url.endsWith("/api/conversations/s2")) {
          return new Response(
            JSON.stringify({
              session_id: "s2",
              agent_type: "demo-assistant",
              messages: [
                {
                  role: "user",
                  content: "帮我整理一下",
                  is_ask_user: false,
                  created_at: "2026-08-03T09:05:00+00:00",
                },
              ],
            }),
            { status: 200 },
          );
        }
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads the full history and binds to the original agent_type", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    render(<App />);

    const item = await screen.findByTestId("conversation-s1");
    await userEvent.click(item);

    expect(await screen.findByText("你好")).toBeInTheDocument();
    expect(screen.getByText("planner")).toBeInTheDocument();
  });

  it("switching directly between two already-open conversations shows the newly-selected one's history, not stale state", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    render(<App />);

    await userEvent.click(await screen.findByTestId("conversation-s1"));
    expect(await screen.findByText("你好")).toBeInTheDocument();

    await userEvent.click(await screen.findByTestId("conversation-s2"));

    expect(await screen.findByText("帮我整理一下")).toBeInTheDocument();
    expect(screen.queryByText("你好")).not.toBeInTheDocument();
    expect(screen.getByText("demo-assistant")).toBeInTheDocument();
  });
});

describe("App: REST-level failures", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a styled notice when starting a conversation fails (agent unavailable)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/agents")) {
          return new Response(JSON.stringify({ agent_types: ["planner"] }), {
            status: 200,
          });
        }
        if (url.endsWith("/api/conversations") && init?.method === "POST") {
          return new Response(JSON.stringify({ error: "该助手当前不可用" }), {
            status: 409,
          });
        }
        if (url.endsWith("/api/conversations")) {
          return new Response(JSON.stringify({ conversations: [] }), { status: 200 });
        }
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );

    const { default: userEvent } = await import("@testing-library/user-event");
    render(<App />);

    await userEvent.click(await screen.findByText("planner"));

    expect(await screen.findByText("该助手当前不可用")).toBeInTheDocument();
  });

  it("shows a styled notice when opening a conversation fails (not found)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/agents")) {
          return new Response(JSON.stringify({ agent_types: [] }), { status: 200 });
        }
        if (url.endsWith("/api/conversations")) {
          return new Response(
            JSON.stringify({
              conversations: [
                {
                  session_id: "gone",
                  agent_type: "planner",
                  title: "已被删除",
                  last_active_at: null,
                },
              ],
            }),
            { status: 200 },
          );
        }
        if (url.endsWith("/api/conversations/gone")) {
          return new Response(JSON.stringify({ error: "conversation not found" }), {
            status: 404,
          });
        }
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );

    const { default: userEvent } = await import("@testing-library/user-event");
    render(<App />);

    await userEvent.click(await screen.findByTestId("conversation-gone"));

    expect(await screen.findByText("conversation not found")).toBeInTheDocument();
  });
});
