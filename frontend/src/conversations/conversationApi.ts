import { apiRequest } from "../api/http";

export type ConversationStatus = "active" | "deletion_pending";
export type ConversationRole = "user" | "assistant";

export interface ConversationSummary {
  conversation_id: string;
  session_id: string;
  title: string | null;
  status: ConversationStatus;
  created_at: string;
  updated_at: string;
  last_message_at: string | null;
}

export interface ConversationListPage {
  items: ConversationSummary[];
  next_cursor: string | null;
}

export interface ConversationMessage {
  turn_id: string;
  role: ConversationRole;
  content: string;
  timestamp: string;
}

export interface ConversationHistoryPage {
  items: ConversationMessage[];
  next_before_message_id: number | null;
}

export function listConversations(
  options: { limit?: number; cursor?: string; signal?: AbortSignal } = {},
): Promise<ConversationListPage> {
  const search = new URLSearchParams({ limit: String(options.limit ?? 20) });
  if (options.cursor !== undefined) {
    search.set("cursor", options.cursor);
  }
  return apiRequest<ConversationListPage>(
    `/api/v1/conversations?${search.toString()}`,
    options.signal === undefined ? {} : { signal: options.signal },
  );
}

export function createConversation(title?: string): Promise<ConversationSummary> {
  return apiRequest<ConversationSummary>("/api/v1/conversations", {
    method: "POST",
    body: title === undefined ? {} : { title },
    csrf: true,
  });
}

export function readConversationHistory(
  sessionId: string,
  options: { limit?: number; beforeMessageId?: number; signal?: AbortSignal } = {},
): Promise<ConversationHistoryPage> {
  const search = new URLSearchParams({ limit: String(options.limit ?? 50) });
  if (options.beforeMessageId !== undefined) {
    search.set("before_message_id", String(options.beforeMessageId));
  }
  return apiRequest<ConversationHistoryPage>(
    `/api/v1/conversations/${encodeURIComponent(sessionId)}/messages?${search.toString()}`,
    options.signal === undefined ? {} : { signal: options.signal },
  );
}

export async function deleteConversation(sessionId: string): Promise<void> {
  await apiRequest<undefined>(`/api/v1/conversations/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
    csrf: true,
  });
}
