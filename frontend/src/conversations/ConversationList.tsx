import { useCallback, useState } from "react";
import { NavLink, useNavigate, useParams } from "react-router-dom";

import { FormError } from "../components/FormError";
import type { ConversationSummary } from "./conversationApi";
import { DeleteConversationDialog } from "./DeleteConversationDialog";
import { conversationTitle } from "./conversationLabels";
import { useConversations } from "./useConversations";

const DATE_FORMAT = new Intl.DateTimeFormat("vi-VN", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});

export function ConversationList() {
  const state = useConversations();
  const navigate = useNavigate();
  const { sessionId: openSessionId } = useParams<{ sessionId: string }>();
  const [selected, setSelected] = useState<ConversationSummary | null>(null);

  const closeDialog = useCallback(() => {
    setSelected(null);
  }, []);

  const confirmDelete = useCallback(async () => {
    if (selected === null) {
      return;
    }
    const sessionId = selected.session_id;
    try {
      await state.remove(sessionId);
      setSelected(null);
      if (openSessionId === sessionId) {
        void navigate("/chat/new", { replace: true });
      }
    } catch {
      setSelected(null);
    }
  }, [navigate, openSessionId, selected, state]);

  return (
    <>
      <div className="conversation-list-header">
        <h2>Cuộc trò chuyện</h2>
        <span>{state.conversations.length}</span>
      </div>

      {state.status === "loading" ? <ConversationListSkeleton /> : null}

      {state.status === "error" ? (
        <div className="sidebar-state">
          <FormError error={state.error} />
          <button className="secondary-button compact-button" type="button" onClick={() => void state.refresh()}>
            Tải lại
          </button>
        </div>
      ) : null}

      {state.status === "ready" && state.error ? (
        <div className="sidebar-state inline-sidebar-error">
          <FormError error={state.error} />
          <button
            className="secondary-button compact-button"
            type="button"
            onClick={() => void (state.nextCursor === null ? state.refresh() : state.loadMore())}
          >
            Thử lại
          </button>
        </div>
      ) : null}

      {state.status === "ready" && state.conversations.length === 0 ? (
        <p className="sidebar-empty">Chưa có lịch sử trò chuyện.</p>
      ) : null}

      {state.conversations.length > 0 ? (
        <ul className="conversation-list" aria-label="Lịch sử trò chuyện">
          {state.conversations.map((conversation) => (
            <ConversationListItem
              key={conversation.session_id}
              conversation={conversation}
              deleting={state.deleting.has(conversation.session_id)}
              deletionError={state.deletionErrors.get(conversation.session_id)}
              onDelete={() => {
                setSelected(conversation);
              }}
              onRetry={() => void state.remove(conversation.session_id).catch(() => undefined)}
            />
          ))}
        </ul>
      ) : null}

      {state.nextCursor !== null ? (
        <button
          className="load-more-button"
          type="button"
          disabled={state.loadingMore}
          onClick={() => void state.loadMore()}
        >
          {state.loadingMore ? "Đang tải" : "Xem thêm"}
        </button>
      ) : null}

      {selected !== null ? (
        <DeleteConversationDialog
          conversation={selected}
          busy={state.deleting.has(selected.session_id)}
          onCancel={closeDialog}
          onConfirm={() => void confirmDelete()}
        />
      ) : null}
    </>
  );
}

function ConversationListItem({
  conversation,
  deleting,
  deletionError,
  onDelete,
  onRetry,
}: {
  conversation: ConversationSummary;
  deleting: boolean;
  deletionError: unknown;
  onDelete: () => void;
  onRetry: () => void;
}) {
  const pending = conversation.status === "deletion_pending";
  const activityAt = conversation.last_message_at ?? conversation.created_at;

  return (
    <li className={pending ? "conversation-item pending" : "conversation-item"}>
      {pending ? (
        <div className="conversation-summary" aria-label={conversationTitle(conversation)}>
          <strong>{conversationTitle(conversation)}</strong>
          <span>{deleting ? "Đang thử xóa lại" : "Đang chờ xóa"}</span>
        </div>
      ) : (
        <NavLink className="conversation-link" to={`/chat/${conversation.session_id}`}>
          <strong>{conversationTitle(conversation)}</strong>
          <time dateTime={activityAt}>{formatActivityDate(activityAt)}</time>
        </NavLink>
      )}
      <div className="conversation-item-actions">
        {pending ? (
          <button type="button" disabled={deleting} onClick={onRetry}>
            Thử lại
          </button>
        ) : (
          <button type="button" onClick={onDelete} aria-label={`Xóa ${conversationTitle(conversation)}`}>
            Xóa
          </button>
        )}
      </div>
      {deletionError ? <span className="conversation-delete-error">Chưa thể xóa. Hãy thử lại.</span> : null}
    </li>
  );
}

function ConversationListSkeleton() {
  return (
    <div className="conversation-skeleton" aria-label="Đang tải lịch sử" role="status">
      <span />
      <span />
      <span />
    </div>
  );
}

function formatActivityDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "" : DATE_FORMAT.format(parsed);
}
