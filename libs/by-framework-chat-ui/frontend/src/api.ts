export interface ConversationSummary {
  session_id: string;
  agent_type: string;
  title: string;
  last_active_at: string | null;
}

export interface HistoryMessage {
  role: string;
  content: string;
  is_ask_user: boolean;
}

export interface ConversationDetail {
  session_id: string;
  agent_type: string;
  messages: HistoryMessage[];
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`${path} failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function listAgentTypes(): Promise<string[]> {
  const body = await getJson<{ agent_types: string[] }>("/api/agents");
  return body.agent_types;
}

export async function listConversations(): Promise<ConversationSummary[]> {
  const body = await getJson<{ conversations: ConversationSummary[] }>("/api/conversations");
  return body.conversations;
}

export async function getConversation(sessionId: string): Promise<ConversationDetail> {
  return getJson<ConversationDetail>(`/api/conversations/${sessionId}`);
}

export async function createConversation(
  agentType: string,
): Promise<{ session_id: string; agent_type: string }> {
  const response = await fetch("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_type: agentType }),
  });
  if (!response.ok) {
    throw new Error(`create conversation failed: ${response.status}`);
  }
  return response.json();
}
