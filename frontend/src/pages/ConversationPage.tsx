import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError } from "../api/http";
import { FormError } from "../components/FormError";
import {
  readConversationHistory,
  type ConversationMessage,
} from "../conversations/conversationApi";
import { conversationTitle } from "../conversations/conversationLabels";
import { useConversations } from "../conversations/useConversations";

export function ConversationPage() {
  const { sessionId } = useParams<{ sessionId: string }>();

  if (sessionId === undefined) {
    return null;
  }
  return <ConversationSession key={sessionId} sessionId={sessionId} />;
}

function ConversationSession({ sessionId }: { sessionId: string }) {
  const conversations = useConversations();
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [nextBeforeMessageId, setNextBeforeMessageId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const current = useMemo(
    () => conversations.conversations.find((item) => item.session_id === sessionId),
    [conversations.conversations, sessionId],
  );

  const loadHistory = useCallback(async (beforeMessageId?: number, signal?: AbortSignal) => {
    const options: { beforeMessageId?: number; signal?: AbortSignal } = {};
    if (beforeMessageId !== undefined) {
      options.beforeMessageId = beforeMessageId;
    }
    if (signal !== undefined) {
      options.signal = signal;
    }
    const page = await readConversationHistory(sessionId, options);
    setMessages((existing) =>
      beforeMessageId === undefined ? page.items : mergeMessages(page.items, existing),
    );
    setNextBeforeMessageId(page.next_before_message_id);
  }, [sessionId]);

  useEffect(() => {
    const controller = new AbortController();
    const bootstrap = async () => {
      try {
        const page = await readConversationHistory(sessionId, { signal: controller.signal });
        setMessages(page.items);
        setNextBeforeMessageId(page.next_before_message_id);
      } catch (caught) {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) {
          setError(caught);
        }
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      }
    };
    void bootstrap();
    return () => {
      controller.abort();
    };
  }, [sessionId]);

  const loadOlder = async () => {
    if (nextBeforeMessageId === null || loadingOlder) {
      return;
    }
    setLoadingOlder(true);
    setError(null);
    try {
      await loadHistory(nextBeforeMessageId);
    } catch (caught) {
      setError(caught);
    } finally {
      setLoadingOlder(false);
    }
  };

  const retryHistory = async () => {
    setLoading(true);
    setError(null);
    try {
      await loadHistory();
    } catch (caught) {
      setError(caught);
    } finally {
      setLoading(false);
    }
  };

  if (current?.status === "deletion_pending") {
    return (
      <section className="conversation-state" aria-labelledby="conversation-pending-title">
        <h1 id="conversation-pending-title">Cuộc trò chuyện đang chờ xóa</h1>
        <p>Thử lại thao tác xóa từ danh sách bên trái.</p>
        <Link className="secondary-link" to="/chat/new">Về cuộc trò chuyện mới</Link>
      </section>
    );
  }

  return (
    <section className="conversation-view" aria-labelledby="conversation-title">
      <header className="conversation-view-header">
        <p className="route-kicker">Lịch sử trò chuyện</p>
        <h1 id="conversation-title">
          {current === undefined ? "Cuộc trò chuyện" : conversationTitle(current)}
        </h1>
      </header>

      {loading ? <HistorySkeleton /> : null}

      {error ? (
        <div className="conversation-state">
          <FormError error={error} />
          {error instanceof ApiError && error.code === "CONVERSATION_NOT_FOUND" ? (
            <Link className="secondary-link" to="/chat/new">Về cuộc trò chuyện mới</Link>
          ) : (
            <button className="secondary-button" type="button" onClick={() => void retryHistory()}>
              Thử lại
            </button>
          )}
        </div>
      ) : null}

      {!loading && error === null && messages.length === 0 ? (
        <div className="conversation-state compact-state">
          <p>Cuộc trò chuyện này chưa có tin nhắn.</p>
          <p>Khung gửi tin sẽ được bật ở bước streaming.</p>
        </div>
      ) : null}

      {messages.length > 0 ? (
        <>
          {nextBeforeMessageId !== null ? (
            <button
              className="load-history-button"
              type="button"
              disabled={loadingOlder}
              onClick={() => void loadOlder()}
            >
              {loadingOlder ? "Đang tải" : "Tải tin nhắn cũ hơn"}
            </button>
          ) : null}
          <ol className="message-list" aria-label="Tin nhắn trong cuộc trò chuyện">
            {messages.map((message) => (
              <li
                className={`message-row ${message.role}`}
                key={`${message.turn_id}:${message.role}:${message.timestamp}`}
              >
                <article className="message-bubble">
                  <span className="message-role">
                    {message.role === "user" ? "Bạn" : "KiRa"}
                  </span>
                  <p>{message.content}</p>
                  <time dateTime={message.timestamp}>{formatMessageTime(message.timestamp)}</time>
                </article>
              </li>
            ))}
          </ol>
        </>
      ) : null}
    </section>
  );
}

function HistorySkeleton() {
  return (
    <div className="history-skeleton" aria-label="Đang tải tin nhắn" role="status">
      <span />
      <span />
      <span />
    </div>
  );
}

function mergeMessages(
  older: ConversationMessage[],
  current: ConversationMessage[],
): ConversationMessage[] {
  const seen = new Set<string>();
  return [...older, ...current].filter((message) => {
    const key = `${message.turn_id}:${message.role}:${message.timestamp}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function formatMessageTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "";
  }
  return new Intl.DateTimeFormat("vi-VN", {
    hour: "2-digit",
    minute: "2-digit",
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(parsed);
}
