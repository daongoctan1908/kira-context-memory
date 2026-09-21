import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { ApiError } from "../api/http";
import { ChatComposer } from "../chat/ChatComposer";
import { MessageActions } from "../chat/MessageActions";
import { SafeMarkdown } from "../chat/SafeMarkdown";
import { useChatStream, type LiveTurn } from "../chat/useChatStream";
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
  const location = useLocation();
  const navigate = useNavigate();
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [nextBeforeMessageId, setNextBeforeMessageId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  const scrollViewport = useRef<HTMLDivElement>(null);
  const bottomAnchor = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const initialHistoryPositioned = useRef(false);
  const initialMessage = useRef(readInitialMessage(location.state));
  const initialMessageSent = useRef(false);
  const touchConversation = conversations.touch;
  const markConversationActive = useCallback(() => {
    touchConversation(sessionId);
  }, [sessionId, touchConversation]);
  const chat = useChatStream({ sessionId, onActivity: markConversationActive });
  const sendChatMessage = chat.send;
  const current = useMemo(
    () => conversations.conversations.find((item) => item.session_id === sessionId),
    [conversations.conversations, sessionId],
  );
  const newestTurn = chat.turns[chat.turns.length - 1];
  const streamedCharacterCount = newestTurn?.assistantText.length ?? 0;
  const newestTurnStatus = newestTurn?.status;
  const hasNewResponse = showScrollToBottom && streamedCharacterCount > 0;

  useLayoutEffect(() => {
    const viewport = scrollViewport.current;
    const positionInitialHistory = !loading && !initialHistoryPositioned.current;
    if (positionInitialHistory) {
      initialHistoryPositioned.current = true;
      stickToBottom.current = true;
    }
    if (viewport !== null && (stickToBottom.current || positionInitialHistory)) {
      viewport.scrollTop = viewport.scrollHeight;
      scrollAnchorIntoView(bottomAnchor.current, { block: "end" });
      setShowScrollToBottom(false);
      const frame = window.requestAnimationFrame(() => {
        if ((stickToBottom.current || positionInitialHistory) && scrollViewport.current !== null) {
          scrollViewport.current.scrollTop = scrollViewport.current.scrollHeight;
          scrollAnchorIntoView(bottomAnchor.current, { block: "end" });
        }
      });
      return () => { window.cancelAnimationFrame(frame); };
    }
    return undefined;
  }, [loading, messages.length, chat.turns.length, streamedCharacterCount, newestTurnStatus]);

  const trackScrollPosition = useCallback(() => {
    const viewport = scrollViewport.current;
    if (viewport === null) {
      return;
    }
    const distanceFromBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
    const nearBottom = distanceFromBottom <= 96;
    stickToBottom.current = nearBottom;
    setShowScrollToBottom(!nearBottom);
  }, []);

  const scrollToBottom = useCallback(() => {
    const viewport = scrollViewport.current;
    if (viewport === null) {
      return;
    }
    stickToBottom.current = true;
    scrollAnchorIntoView(bottomAnchor.current, { block: "end", behavior: "smooth" });
    viewport.scrollTo({ top: viewport.scrollHeight, behavior: "smooth" });
    setShowScrollToBottom(false);
  }, []);

  const sendMessage = useCallback((message: string) => {
    stickToBottom.current = true;
    sendChatMessage(message);
  }, [sendChatMessage]);

  useEffect(() => {
    if (
      loading
      || error !== null
      || initialMessageSent.current
      || initialMessage.current === null
    ) {
      return;
    }
    initialMessageSent.current = true;
    const message = initialMessage.current;
    initialMessage.current = null;
    void navigate(location.pathname, { replace: true, state: null });
    sendMessage(message);
  }, [error, loading, location.pathname, navigate, sendMessage]);

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
    const viewport = scrollViewport.current;
    const previousHeight = viewport?.scrollHeight ?? 0;
    const previousTop = viewport?.scrollTop ?? 0;
    try {
      await loadHistory(nextBeforeMessageId);
      window.requestAnimationFrame(() => {
        const currentViewport = scrollViewport.current;
        if (currentViewport !== null) {
          currentViewport.scrollTop = previousTop + currentViewport.scrollHeight - previousHeight;
        }
      });
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
    <section className="conversation-view" aria-label={current === undefined ? "Cuộc trò chuyện" : conversationTitle(current)}>
      <div className="conversation-scroll-shell">
        <div
          className="conversation-scroll"
          ref={scrollViewport}
          onScroll={trackScrollPosition}
        >
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

        {!loading && error === null && messages.length === 0 && chat.turns.length === 0 ? (
          <div className="conversation-state compact-state">
            <p>Cuộc trò chuyện này chưa có tin nhắn.</p>
            <p>Gửi câu hỏi đầu tiên để bắt đầu.</p>
          </div>
        ) : null}

        {!loading && error === null && nextBeforeMessageId !== null ? (
          <button
            className="load-history-button"
            type="button"
            disabled={loadingOlder}
            onClick={() => void loadOlder()}
          >
            {loadingOlder ? "Đang tải" : "Tải tin nhắn cũ hơn"}
          </button>
        ) : null}

        {messages.length > 0 || chat.turns.length > 0 ? (
          <ol
            className="message-list"
            aria-label="Tin nhắn trong cuộc trò chuyện"
            aria-live="polite"
          >
            {messages.map((message) => (
              <li
                className={`message-row ${message.role}`}
                key={`${message.turn_id}:${message.role}:${message.timestamp}`}
              >
                <article className="message-bubble">
                  {message.role === "assistant" ? (
                    <SafeMarkdown content={message.content} />
                  ) : (
                    <p>{message.content}</p>
                  )}
                  <time className="message-time" dateTime={message.timestamp}>{formatMessageTime(message.timestamp)}</time>
                  {message.role === "assistant" ? (
                    <MessageActions
                      key={`${message.turn_id}:${message.feedback ?? "none"}`}
                      sessionId={sessionId}
                      turnId={message.turn_id}
                      content={message.content}
                      initialRating={message.feedback ?? null}
                    />
                  ) : null}
                </article>
              </li>
            ))}
            {chat.turns.map((turn) => (
              <LiveTurnMessages
                key={turn.clientMessageId}
                sessionId={sessionId}
                turn={turn}
                active={chat.activeClientMessageId === turn.clientMessageId}
                onRetry={() => {
                  stickToBottom.current = true;
                  chat.retry(turn.clientMessageId);
                }}
              />
            ))}
          </ol>
        ) : null}
        <div className="conversation-bottom-anchor" ref={bottomAnchor} aria-hidden="true" />
        </div>

        {showScrollToBottom ? (
          <button
            className="scroll-to-bottom"
            type="button"
            onClick={scrollToBottom}
            aria-label="Cuộn xuống tin nhắn mới nhất"
            title="Xuống tin nhắn mới nhất"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M5 9l7 7 7-7" />
            </svg>
            {hasNewResponse ? <span className="new-response-indicator">Mới</span> : null}
          </button>
        ) : null}
      </div>

      {!loading && error === null ? (
        <ChatComposer sessionId={sessionId} streaming={chat.isStreaming} onSend={sendMessage} onStop={chat.stop} />
      ) : null}
    </section>
  );
}

function LiveTurnMessages({
  sessionId,
  turn,
  active,
  onRetry,
}: {
  sessionId: string;
  turn: LiveTurn;
  active: boolean;
  onRetry: () => void;
}) {
  return (
    <>
      <li className="message-row user" data-client-message-id={turn.clientMessageId}>
        <article className="message-bubble">
          <p>{turn.userText}</p>
        </article>
      </li>
      <li className="message-row assistant" data-client-message-id={turn.clientMessageId}>
        <article className="message-bubble live-message-bubble">
          {turn.assistantText.length > 0 ? (
            <SafeMarkdown content={turn.assistantText} />
          ) : (
            active ? <StreamingIndicator /> : <p className="stream-placeholder">Chưa nhận được câu trả lời.</p>
          )}
          <TurnStatus turn={turn} />
          {turn.assistantText.length > 0 && turn.status !== "streaming" ? (
            turn.turnId === null ? null : (
              <MessageActions
                sessionId={sessionId}
                turnId={turn.turnId}
                content={turn.assistantText}
              />
            )
          ) : null}
          {turn.retryable && !active ? (
            <button className="secondary-button compact-button" type="button" onClick={onRetry}>
              Thử gửi lại
            </button>
          ) : null}
        </article>
      </li>
    </>
  );
}

function StreamingIndicator() {
  return (
    <div className="streaming-indicator" role="status" aria-label="KiRa đang trả lời">
      <span /><span /><span />
    </div>
  );
}

function TurnStatus({ turn }: { turn: LiveTurn }) {
  if (turn.status === "completed") {
    return turn.replayed ? (
      <p className="turn-status success">Đã khôi phục câu trả lời đã lưu.</p>
    ) : null;
  }
  if (turn.status === "unsaved") {
    return (
      <div className="turn-warning" role="alert">
        Câu trả lời phía trên chưa được xác nhận đã lưu. Thử gửi lại sẽ dùng đúng mã yêu cầu cũ.
      </div>
    );
  }
  if (turn.status === "stopped") {
    return <div className="turn-warning">Đã dừng trước khi lưu câu trả lời.</div>;
  }
  if (turn.status === "failed") {
    return <FormError error={turn.error} />;
  }
  return null;
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

function readInitialMessage(state: unknown): string | null {
  if (typeof state !== "object" || state === null || !("initialMessage" in state)) {
    return null;
  }
  const message = (state as { initialMessage?: unknown }).initialMessage;
  return typeof message === "string" && message.trim().length > 0 ? message.trim() : null;
}

function scrollAnchorIntoView(
  anchor: HTMLDivElement | null,
  options: ScrollIntoViewOptions,
): void {
  if (anchor === null) {
    return;
  }
  const scrollMethod: unknown = Reflect.get(anchor, "scrollIntoView");
  if (typeof scrollMethod === "function") {
    Reflect.apply(scrollMethod, anchor, [options]);
  }
}
