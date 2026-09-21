import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  createConversation,
  deleteConversation,
  listConversations,
  renameConversation,
  type ConversationSummary,
} from "./conversationApi";
import {
  ConversationContext,
  type ConversationContextValue,
  type ConversationListStatus,
} from "./ConversationContext";

export function ConversationProvider({ children }: { children: ReactNode }) {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [status, setStatus] = useState<ConversationListStatus>("loading");
  const [error, setError] = useState<unknown>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [deleting, setDeleting] = useState<ReadonlySet<string>>(new Set());
  const [deletionErrors, setDeletionErrors] = useState<ReadonlyMap<string, unknown>>(new Map());
  const activeRequest = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setStatus("loading");
    setError(null);
    try {
      const page = await listConversations({ signal: controller.signal });
      setConversations(page.items);
      setNextCursor(page.next_cursor);
      setStatus("ready");
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") {
        return;
      }
      setError(caught);
      setStatus("error");
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => {
      activeRequest.current?.abort();
    };
  }, [refresh]);

  const loadMore = useCallback(async () => {
    if (nextCursor === null || loadingMore) {
      return;
    }
    setLoadingMore(true);
    setError(null);
    try {
      const page = await listConversations({ cursor: nextCursor });
      setConversations((current) => mergeConversations(current, page.items));
      setNextCursor(page.next_cursor);
    } catch (caught) {
      setError(caught);
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, nextCursor]);

  const create = useCallback(async (title?: string) => {
    const created = await createConversation(title);
    setConversations((current) => mergeConversations([created], current));
    return created;
  }, []);

  const rename = useCallback(async (sessionId: string, title: string) => {
    const renamed = await renameConversation(sessionId, title);
    setConversations((current) =>
      current.map((conversation) =>
        conversation.session_id === sessionId ? renamed : conversation,
      ),
    );
    return renamed;
  }, []);

  const touch = useCallback((sessionId: string) => {
    const activityAt = new Date().toISOString();
    setConversations((current) => promoteConversation(current, sessionId, activityAt));
  }, []);

  const remove = useCallback(async (sessionId: string) => {
    setDeleting((current) => new Set(current).add(sessionId));
    setDeletionErrors((current) => withoutMapKey(current, sessionId));
    try {
      await deleteConversation(sessionId);
      setConversations((current) =>
        current.filter((conversation) => conversation.session_id !== sessionId),
      );
      setDeletionErrors((current) => withoutMapKey(current, sessionId));
    } catch (caught) {
      setConversations((current) =>
        current.map((conversation) =>
          conversation.session_id === sessionId
            ? { ...conversation, status: "deletion_pending" }
            : conversation,
        ),
      );
      setDeletionErrors((current) => new Map(current).set(sessionId, caught));
      throw caught;
    } finally {
      setDeleting((current) => withoutSetValue(current, sessionId));
    }
  }, []);

  const value = useMemo<ConversationContextValue>(
    () => ({
      conversations,
      status,
      error,
      nextCursor,
      loadingMore,
      deleting,
      deletionErrors,
      refresh,
      loadMore,
      create,
      rename,
      touch,
      remove,
    }),
    [
      conversations,
      status,
      error,
      nextCursor,
      loadingMore,
      deleting,
      deletionErrors,
      refresh,
      loadMore,
      create,
      rename,
      touch,
      remove,
    ],
  );

  return <ConversationContext.Provider value={value}>{children}</ConversationContext.Provider>;
}

function mergeConversations(
  existing: ConversationSummary[],
  incoming: ConversationSummary[],
): ConversationSummary[] {
  const seen = new Set<string>();
  return [...existing, ...incoming].filter((conversation) => {
    if (seen.has(conversation.session_id)) {
      return false;
    }
    seen.add(conversation.session_id);
    return true;
  });
}

function promoteConversation(
  conversations: ConversationSummary[],
  sessionId: string,
  activityAt: string,
): ConversationSummary[] {
  const index = conversations.findIndex((conversation) => conversation.session_id === sessionId);
  if (index < 0) {
    return conversations;
  }
  const selected = conversations[index];
  if (selected === undefined) {
    return conversations;
  }
  const promoted = {
    ...selected,
    updated_at: activityAt,
    last_message_at: activityAt,
  };
  return [promoted, ...conversations.slice(0, index), ...conversations.slice(index + 1)];
}

function withoutSetValue(values: ReadonlySet<string>, value: string): ReadonlySet<string> {
  const next = new Set(values);
  next.delete(value);
  return next;
}

function withoutMapKey(
  values: ReadonlyMap<string, unknown>,
  key: string,
): ReadonlyMap<string, unknown> {
  const next = new Map(values);
  next.delete(key);
  return next;
}
