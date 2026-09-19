import { createContext } from "react";

import type { ConversationSummary } from "./conversationApi";

export type ConversationListStatus = "loading" | "ready" | "error";

export interface ConversationContextValue {
  conversations: ConversationSummary[];
  status: ConversationListStatus;
  error: unknown;
  nextCursor: string | null;
  loadingMore: boolean;
  deleting: ReadonlySet<string>;
  deletionErrors: ReadonlyMap<string, unknown>;
  refresh: () => Promise<void>;
  loadMore: () => Promise<void>;
  create: (title?: string) => Promise<ConversationSummary>;
  remove: (sessionId: string) => Promise<void>;
}

export const ConversationContext = createContext<ConversationContextValue | null>(null);
