import { useEffect, useRef } from "react";

import type { ConversationSummary } from "./conversationApi";
import { conversationTitle } from "./conversationLabels";

export function DeleteConversationDialog({
  conversation,
  busy,
  onCancel,
  onConfirm,
}: {
  conversation: ConversationSummary;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cancelButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelButton.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        onCancel();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [busy, onCancel]);

  return (
    <div className="dialog-backdrop">
      <section
        className="confirm-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="delete-dialog-title"
        aria-describedby="delete-dialog-description"
      >
        <h2 id="delete-dialog-title">Xóa cuộc trò chuyện?</h2>
        <p id="delete-dialog-description">
          Toàn bộ lịch sử và memory được tạo từ cuộc trò chuyện này sẽ bị xóa vĩnh viễn.
        </p>
        <p className="dialog-subject">{conversationTitle(conversation)}</p>
        <div className="dialog-actions">
          <button
            ref={cancelButton}
            className="secondary-button"
            type="button"
            disabled={busy}
            onClick={onCancel}
          >
            Hủy
          </button>
          <button
            className="danger-button"
            type="button"
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? "Đang xóa" : "Xóa vĩnh viễn"}
          </button>
        </div>
      </section>
    </div>
  );
}
