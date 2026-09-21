import { RotateCcw } from "lucide-react";
import { NavLink, useNavigate, useParams } from "react-router-dom";

import { FormError } from "../components/FormError";
import { ConversationActions } from "./ConversationActions";
import { conversationTitle } from "./conversationLabels";
import { useConversations } from "./useConversations";

const DATE_FORMAT = new Intl.DateTimeFormat("vi-VN", { day: "2-digit", month: "2-digit" });

export function ConversationList() {
  const state = useConversations();
  const navigate = useNavigate();
  const { sessionId: openSessionId } = useParams<{ sessionId: string }>();

  return (
    <div className="conversation-history">
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
          <button className="secondary-button compact-button" type="button" onClick={() => void (state.nextCursor === null ? state.refresh() : state.loadMore())}>
            Thử lại
          </button>
        </div>
      ) : null}
      {state.status === "ready" && state.conversations.length === 0 ? (
        <p className="sidebar-empty">Các cuộc trò chuyện sẽ xuất hiện tại đây.</p>
      ) : null}
      {state.conversations.length > 0 ? (
        <ul className="conversation-list" aria-label="Lịch sử trò chuyện">
            {state.conversations.map((conversation) => {
              const pending = conversation.status === "deletion_pending";
              const deleting = state.deleting.has(conversation.session_id);
              const activityAt = conversation.last_message_at ?? conversation.created_at;
              return (
                <li className={pending ? "conversation-item pending" : "conversation-item"} key={conversation.session_id}>
                  {pending ? (
                    <div className="conversation-summary" aria-label={conversationTitle(conversation)}>
                      <strong>{conversationTitle(conversation)}</strong>
                      <span>{deleting ? "Đang thử xóa lại" : "Đang chờ xóa"}</span>
                    </div>
                  ) : (
                    <NavLink className="conversation-link" to={`/chat/${conversation.session_id}`}>
                      <strong>{conversationTitle(conversation)}</strong>
                      <time dateTime={activityAt}>{DATE_FORMAT.format(new Date(activityAt))}</time>
                    </NavLink>
                  )}
                  <div className="conversation-item-actions">
                    {pending ? (
                      <button className="icon-button" type="button" disabled={deleting} onClick={() => void state.remove(conversation.session_id).catch(() => undefined)} aria-label="Thử xóa lại">
                        <RotateCcw size={16} />
                      </button>
                    ) : (
                      <ConversationActions
                        conversation={conversation}
                        afterDelete={openSessionId === conversation.session_id ? () => void navigate("/chat/new", { replace: true }) : () => undefined}
                      />
                    )}
                  </div>
                  {state.deletionErrors.get(conversation.session_id) ? (
                    <span className="conversation-delete-error">Chưa thể xóa. Hãy thử lại.</span>
                  ) : null}
                </li>
              );
            })}
        </ul>
      ) : null}
      {state.nextCursor !== null ? (
        <button className="load-more-button" type="button" disabled={state.loadingMore} onClick={() => void state.loadMore()}>
          {state.loadingMore ? "Đang tải" : "Xem thêm"}
        </button>
      ) : null}
    </div>
  );
}

function ConversationListSkeleton() {
  return (
    <div className="conversation-skeleton" aria-label="Đang tải lịch sử" role="status">
      <span /><span /><span /><span />
    </div>
  );
}
